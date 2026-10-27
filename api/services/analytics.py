"""Market analytics services ported from the original FTrade bot.

The algorithms live in engine/ and retain their original thresholds. This layer only
fetches source data, caches snapshots in the shared cache, and shapes the public API.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from api.cache import JsonCache
from api.services.market import QUOTES_KEY, utc_iso
from engine import crypto_calendar, derivatives as derivatives_engine, flow as flow_engine, regime as regime_engine
from worker.binance_rest import BinanceDataError, BinanceInvalidSymbolError, BinanceRestClient

logger = logging.getLogger("ftrade.analytics")

DERIVATIVES_TTL = 60
FLOW_TTL = 20
REGIME_TTL = 90
CALENDAR_TTL = 300
DERIVATIVES_KEY_PREFIX = "market:derivatives:v1:"
FLOW_KEY_PREFIX = "market:flow:v1:"
REGIME_KEY = "market:regime:v1"
CALENDAR_KEY_PREFIX = "market:calendar:v1:"
SYMBOL_ALLOWED = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


class AnalyticsUnavailable(RuntimeError):
    """A requested analytics snapshot could not be computed from source data."""


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _iso_from_ms(value: Any) -> str | None:
    timestamp = _finite(value)
    if timestamp is None or timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _validated_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or len(symbol) < 6 or len(symbol) > 24 or not symbol.endswith("USDT") or any(c not in SYMBOL_ALLOWED for c in symbol):
        raise ValueError("INVALID_SYMBOL")
    return derivatives_engine.validate_symbol(symbol)


def _frame_from_klines(rows: list[list[Any]]) -> pd.DataFrame:
    parsed: list[dict[str, float]] = []
    for row in rows or []:
        try:
            item = {
                "open_time": float(row[0]), "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
                "close_time": float(row[6]),
            }
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        if not all(math.isfinite(v) for v in item.values()) or item["close"] <= 0:
            continue
        parsed.append(item)
    return pd.DataFrame(parsed)


def _cvd_candles(candles: list[dict[str, float]], cvd_values: list[float], tz_name: str) -> list[dict[str, Any]]:
    if not candles or not cvd_values:
        return []
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        zone = ZoneInfo("UTC")
    tail = candles[-(240 // flow_engine.CANDLE_MINUTES):]
    points = len(cvd_values)
    step = len(tail) / points
    result = []
    for index, cvd in enumerate(cvd_values):
        candle_index = min(len(tail) - 1, max(0, int(round((index + 1) * step)) - 1))
        candle = tail[candle_index]
        timestamp_ms = int(candle["t"] * 1000)
        label = datetime.fromtimestamp(candle["t"], tz=timezone.utc).astimezone(zone).strftime("%H:%M")
        result.append({
            "time": label, "timestamp": timestamp_ms, "open": candle["o"], "high": candle["h"],
            "low": candle["l"], "close": candle["c"], "volume": candle["qv"], "cvd": cvd,
        })
    return result


class MarketAnalyticsService:
    def __init__(self, cache: JsonCache, rest: BinanceRestClient, *, clock=time.time) -> None:
        self.cache = cache
        self.rest = rest
        self.clock = clock
        self._derivative_locks: dict[str, asyncio.Lock] = {}
        self._flow_locks: dict[str, asyncio.Lock] = {}
        self._regime_lock = asyncio.Lock()
        self._calendar_locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, collection: dict[str, asyncio.Lock], key: str | int) -> asyncio.Lock:
        lock = collection.get(key)
        if lock is None:
            lock = asyncio.Lock()
            collection[key] = lock
        return lock

    async def _safe_endpoint(self, path: str, params: dict[str, Any]) -> Any | None:
        try:
            return await self.rest.fetch_endpoint_json(path, params)
        except Exception as exc:
            logger.debug("Analytics source %s unavailable: %s", path, exc)
            return None

    async def derivatives(self, symbol: str) -> dict[str, Any]:
        symbol = _validated_symbol(symbol)
        key = DERIVATIVES_KEY_PREFIX + symbol
        cached = await self.cache.get_json(key)
        if isinstance(cached, dict) and cached.get("symbol") == symbol:
            return cached

        async with self._lock_for(self._derivative_locks, symbol):
            cached = await self.cache.get_json(key)
            if isinstance(cached, dict) and cached.get("symbol") == symbol:
                return cached

            funding_task = self._safe_endpoint("/fapi/v1/premiumIndex", {"symbol": symbol})
            oi_task = self._safe_endpoint("/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 25})
            ls_task = self._safe_endpoint("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": "1h", "limit": 25})
            top_task = self._safe_endpoint("/futures/data/topLongShortPositionRatio", {"symbol": symbol, "period": "1h", "limit": 1})
            funding, oi_rows, ls_rows, top_rows = await asyncio.gather(funding_task, oi_task, ls_task, top_task)

            f = derivatives_engine._to_float
            raw: dict[str, Any] = {
                "symbol": symbol, "funding": None, "next_funding_ms": None, "premium_pct": None,
                "oi_value": None, "oi_change_1h": None, "oi_change_24h": None,
                "ls_ratio": None, "ls_long_pct": None, "ls_ratio_24h": None, "top_ratio": None,
            }
            if isinstance(funding, dict):
                raw["funding"] = f(funding.get("lastFundingRate"))
                raw["next_funding_ms"] = f(funding.get("nextFundingTime"))
                mark, index = f(funding.get("markPrice")), f(funding.get("indexPrice"))
                if mark is not None and index is not None and index > 0:
                    raw["premium_pct"] = (mark / index - 1.0) * 100.0
            if isinstance(oi_rows, list):
                values = [f(row.get("sumOpenInterestValue")) for row in oi_rows if isinstance(row, dict)]
                values = [value for value in values if value is not None]
                if values:
                    raw["oi_value"] = values[-1]
                    if len(values) >= 2:
                        raw["oi_change_1h"] = derivatives_engine._pct_change(values[-1], values[-2])
                        raw["oi_change_24h"] = derivatives_engine._pct_change(values[-1], values[0])
            if isinstance(ls_rows, list):
                valid_ls = [row for row in ls_rows if isinstance(row, dict)]
                if valid_ls:
                    latest = valid_ls[-1]
                    raw["ls_ratio"] = f(latest.get("longShortRatio"))
                    long_account = f(latest.get("longAccount"))
                    raw["ls_long_pct"] = long_account * 100.0 if long_account is not None else None
                    raw["ls_ratio_24h"] = f(valid_ls[0].get("longShortRatio")) if len(valid_ls) > 1 else None
            if isinstance(top_rows, list):
                valid_top = [row for row in top_rows if isinstance(row, dict)]
                if valid_top:
                    raw["top_ratio"] = f(valid_top[-1].get("longShortRatio"))

            if all(raw[field] is None for field in ("funding", "oi_value", "ls_ratio", "top_ratio")):
                raise AnalyticsUnavailable("DERIVATIVES_UNAVAILABLE")

            long_pct = raw["ls_long_pct"]
            if long_pct is None and raw["ls_ratio"] is not None and raw["ls_ratio"] >= 0:
                long_pct = raw["ls_ratio"] / (1.0 + raw["ls_ratio"]) * 100.0
            short_pct = 100.0 - long_pct if long_pct is not None else None
            top_long = None
            if raw["top_ratio"] is not None and raw["top_ratio"] >= 0:
                top_long = raw["top_ratio"] / (1.0 + raw["top_ratio"]) * 100.0
            top_short = 100.0 - top_long if top_long is not None else None
            updated_at = utc_iso(self.clock())
            output = {
                "symbol": symbol,
                "funding_rate": raw["funding"],
                "funding_next": _iso_from_ms(raw["next_funding_ms"]),
                "mark_index_delta": raw["premium_pct"],
                "open_interest": raw["oi_value"],
                "oi_change_1h": raw["oi_change_1h"],
                "oi_change_24h": raw["oi_change_24h"],
                "long_short_ratio": {"percent_long": long_pct, "percent_short": short_pct, "value_24h_ago": raw["ls_ratio_24h"]},
                "top_trader_ratio": {"long": top_long, "short": top_short},
                "reading": " ".join(derivatives_engine.interpret(raw)),
                "updated_at": updated_at,
                "raw": raw,
            }
            await self.cache.set_json(key, output, ttl_seconds=DERIVATIVES_TTL)
            return output

    async def flow(self, symbol: str, tz_name: str = "UTC") -> dict[str, Any]:
        symbol = _validated_symbol(symbol)
        key = FLOW_KEY_PREFIX + symbol
        cached = await self.cache.get_json(key)
        if isinstance(cached, dict) and cached.get("symbol") == symbol:
            return self._flow_for_timezone(cached, tz_name)

        async with self._lock_for(self._flow_locks, symbol):
            cached = await self.cache.get_json(key)
            if isinstance(cached, dict) and cached.get("symbol") == symbol:
                return self._flow_for_timezone(cached, tz_name)
            rows = await self.rest.fetch_klines(symbol, "5m", flow_engine.CANDLE_LIMIT)
            candles = flow_engine.parse_candles(rows)
            if len(candles) < 49:
                raise AnalyticsUnavailable("FLOW_COLLECTING")
            computed = flow_engine.compute_flow(symbol, candles, now=self.clock())
            windows = []
            for row in computed["windows"]:
                windows.append({
                    "label": row["label"], "minutes": row["minutes"],
                    "buy_pct": row["buy_pct"], "delta": row["delta_base"],
                    "delta_usdt": row["delta_quote"], "price": row["price"],
                    "change_pct": row["price_chg"], "volume_ratio": row.get("volume_ratio"),
                    "volume_usdt": row["volume_quote"],
                })
            output = {
                "symbol": symbol,
                "windows": windows,
                "cvd": _cvd_candles(candles, computed["cvd"], "UTC"),
                "volume_ratio": next((row.get("volume_ratio") for row in windows if row["label"] == "15m"), None),
                "reading": " ".join(flow_engine.interpret_flow(computed)),
                "updated_at": utc_iso(self.clock()),
                "source": "binance_futures_klines_5m",
                "status": "ok",
                "raw": computed,
            }
            await self.cache.set_json(key, output, ttl_seconds=FLOW_TTL)
            return self._flow_for_timezone(output, tz_name)

    @staticmethod
    def _flow_for_timezone(data: dict[str, Any], tz_name: str) -> dict[str, Any]:
        try:
            zone = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            zone = ZoneInfo("UTC")
        result = dict(data)
        cvd = []
        for item in data.get("cvd", []):
            row = dict(item)
            try:
                timestamp = float(row.get("timestamp", 0)) / 1000.0
                row["time"] = datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(zone).strftime("%H:%M")
            except (TypeError, ValueError, OverflowError, OSError):
                pass
            cvd.append(row)
        result["cvd"] = cvd
        return result

    async def calendar(self, days: int = 7) -> dict[str, Any]:
        if days not in (7, 30):
            raise ValueError("INVALID_CALENDAR_WINDOW")
        key = CALENDAR_KEY_PREFIX + str(days)
        cached = await self.cache.get_json(key)
        if isinstance(cached, dict) and isinstance(cached.get("events"), list):
            return cached
        async with self._lock_for(self._calendar_locks, days):
            cached = await self.cache.get_json(key)
            if isinstance(cached, dict) and isinstance(cached.get("events"), list):
                return cached
            bls = await asyncio.to_thread(crypto_calendar.get_bls_events)
            now = datetime.now(timezone.utc)
            snapshot = crypto_calendar.build_calendar(now=now, days=days, min_impact="bajo", bls=bls)
            events: list[dict[str, Any]] = []
            now_epoch = now.timestamp()
            for index, event in enumerate(snapshot["events"]):
                when = event["when"].astimezone(timezone.utc)
                events.append({
                    "id": f"{event['kind']}:{int(when.timestamp())}:{index}",
                    "title": event["title"], "date": when.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "impact": event["impact"], "category": event["kind"].upper(),
                    "countdown": max(0, int(when.timestamp() - now_epoch)), "note": event.get("note", ""),
                })
            # The next BTC funding timestamp is read from the exchange, not inferred unless unavailable.
            premium = await self._safe_endpoint("/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"})
            if isinstance(premium, dict):
                funding_ms = _finite(premium.get("nextFundingTime"))
                funding_dt = datetime.fromtimestamp(funding_ms / 1000, tz=timezone.utc) if funding_ms and funding_ms > 0 else None
                if funding_dt and now <= funding_dt <= now + pd.Timedelta(days=days).to_pytimedelta():
                    events.append({
                        "id": f"funding:{int(funding_dt.timestamp())}", "title": "Próximo funding de Binance Futures",
                        "date": funding_dt.isoformat(timespec="seconds").replace("+00:00", "Z"),
                        "impact": "bajo", "category": "FUNDING", "countdown": max(0, int(funding_dt.timestamp() - now_epoch)),
                        "note": "Horario obtenido del endpoint premiumIndex de BTCUSDT.",
                    })
            events.sort(key=lambda event: event["date"])
            result = {"events": events, "bls_ok": bool(snapshot.get("bls_ok")), "days": days, "updated_at": utc_iso(self.clock())}
            await self.cache.set_json(key, result, ttl_seconds=CALENDAR_TTL)
            return result

    async def regime(self) -> dict[str, Any]:
        cached = await self.cache.get_json(REGIME_KEY)
        if isinstance(cached, dict) and cached.get("updated_at"):
            try:
                age = self.clock() - datetime.fromisoformat(cached["updated_at"].replace("Z", "+00:00")).timestamp()
                if 0 <= age < REGIME_TTL:
                    return cached
            except (ValueError, TypeError):
                pass
        async with self._regime_lock:
            cached = await self.cache.get_json(REGIME_KEY)
            if isinstance(cached, dict) and cached.get("updated_at"):
                try:
                    age = self.clock() - datetime.fromisoformat(cached["updated_at"].replace("Z", "+00:00")).timestamp()
                    if 0 <= age < REGIME_TTL:
                        return cached
                except (ValueError, TypeError):
                    pass
            frames: dict[str, pd.DataFrame | None] = {}
            frame_tasks = {
                tf: self.rest.fetch_klines("BTCUSDT", interval, 220)
                for tf, interval in (("15m", "15m"), ("1h", "1h"), ("4h", "4h"))
            }
            results = await asyncio.gather(*frame_tasks.values(), return_exceptions=True)
            for (tf, _interval), result in zip((("15m", "15m"), ("1h", "1h"), ("4h", "4h")), results):
                frames[tf] = None if isinstance(result, Exception) else _frame_from_klines(result)
            if frames.get("1h") is None or frames["1h"].empty:
                if isinstance(cached, dict) and cached.get("updated_at"):
                    fallback = dict(cached)
                    fallback["status"] = "stale"
                    fallback["message"] = "Se muestra el último régimen calculado; Binance no ha respondido con velas recientes."
                    return fallback
                raise AnalyticsUnavailable("REGIME_DATA_UNAVAILABLE")

            breadth = await self._breadth(frames.get("1h"))
            try:
                # Funding/OI enrich the regime but must not hold the entire screen
                # hostage when Binance's secondary endpoints are slow.
                derivatives = await asyncio.wait_for(self.derivatives("BTCUSDT"), timeout=3.0)
                raw_derivatives = derivatives.get("raw") or {}
            except Exception as exc:
                logger.info("Regime derivatives context unavailable or slow: %s", exc)
                derivatives, raw_derivatives = {}, {}
            computed = await asyncio.to_thread(
                regime_engine.compute_market_regime, frames, "BTCUSDT", breadth,
                raw_derivatives.get("funding"), raw_derivatives.get("oi_change_24h"),
                datetime.now(timezone.utc),
            )
            if not computed:
                raise AnalyticsUnavailable("REGIME_HISTORY_INSUFFICIENT")
            tf1h = computed.get("timeframes", {}).get("1h", {})
            trend_map = {
                regime_engine.REGIME_UP: "TENDENCIA ALCISTA",
                regime_engine.REGIME_DOWN: "TENDENCIA BAJISTA",
                regime_engine.REGIME_RANGE: "RANGO",
                regime_engine.REGIME_TRANSITION: "TRANSICIÓN",
                regime_engine.REGIME_VOLATILE: "ALTA VOLATILIDAD",
            }
            updated_at = utc_iso(self.clock())
            output = {
                "trend": trend_map.get(computed["regime"], "TRANSICIÓN"),
                "regime": computed["regime"], "adx": tf1h.get("adx"), "atr_pct": tf1h.get("atr_pct"),
                "bollinger_state": tf1h.get("bandwidth_trend"), "volume_relative": tf1h.get("rel_volume"),
                "funding_rate": raw_derivatives.get("funding"), "oi_value": raw_derivatives.get("oi_value"),
                "market_breadth": (breadth.get("bullish_pct") / 100.0) if breadth else None,
                "session_now": computed.get("session", {}).get("name", "Desconocida"),
                "updated_at": updated_at, "confidence": computed.get("confidence"), "score": computed.get("score"),
                "volatility_label": computed.get("volatility_label"), "timeframes": computed.get("timeframes"),
                "reference": computed.get("reference"), "breadth": breadth, "guidance": computed.get("guidance"),
            }
            await self.cache.set_json(REGIME_KEY, output, ttl_seconds=REGIME_TTL)
            return output

    async def _breadth(self, btc_1h: pd.DataFrame | None) -> dict[str, Any] | None:
        cached_quotes = await self.cache.get_json(QUOTES_KEY)
        rows: list[dict[str, Any]] = []
        if isinstance(cached_quotes, dict):
            rows = [dict(value, symbol=key) for key, value in cached_quotes.items() if isinstance(value, dict)]
        if not rows:
            try:
                rows = await self.rest.fetch_24h_tickers()
            except Exception as exc:
                logger.debug("No ticker list for market breadth: %s", exc)
                return None
        rows = [r for r in rows if str(r.get("symbol") or r.get("s") or "").upper().endswith("USDT")]
        rows.sort(key=lambda r: _finite(r.get("volume_24h", r.get("quoteVolume", r.get("q")))) or 0.0, reverse=True)
        symbols = list(dict.fromkeys(str(r.get("symbol") or r.get("s") or "").upper() for r in rows))[:12]
        states: list[dict[str, bool] | None] = []
        for symbol in symbols:
            if symbol == "BTCUSDT" and btc_1h is not None:
                states.append(regime_engine.symbol_trend_state(btc_1h))
                continue
            states.append(None)
        missing = [symbol for symbol in symbols if symbol != "BTCUSDT"][:11]
        async def state_for(symbol: str):
            try:
                klines = await self.rest.fetch_klines(symbol, "1h", 70)
                return regime_engine.symbol_trend_state(_frame_from_klines(klines))
            except Exception:
                return None
        loaded = await asyncio.gather(*(state_for(symbol) for symbol in missing), return_exceptions=True)
        iterator = iter(loaded)
        ordered: list[dict[str, bool] | None] = []
        for symbol in symbols:
            if symbol == "BTCUSDT" and btc_1h is not None:
                ordered.append(regime_engine.symbol_trend_state(btc_1h))
            else:
                value = next(iterator, None)
                ordered.append(None if isinstance(value, Exception) else value)
        return regime_engine.summarize_breadth(ordered)
