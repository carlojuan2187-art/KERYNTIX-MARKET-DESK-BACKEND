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
ORDERBOOK_TTL = 5
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
        """Return a full, freshness-aware derivatives snapshot using real Binance data."""
        symbol = _validated_symbol(symbol)
        key = DERIVATIVES_KEY_PREFIX + symbol
        cached = await self.cache.get_json(key)
        if isinstance(cached, dict) and cached.get("symbol") == symbol:
            return cached

        async with self._lock_for(self._derivative_locks, symbol):
            cached = await self.cache.get_json(key)
            if isinstance(cached, dict) and cached.get("symbol") == symbol:
                return cached

            now = self.clock()
            key_configured = bool(getattr(self.rest, "api_key_configured", False))
            requests: list[tuple[str, Any]] = [
                ("funding", self._safe_endpoint("/fapi/v1/premiumIndex", {"symbol": symbol})),
                ("open_interest", self._safe_endpoint("/fapi/v1/openInterest", {"symbol": symbol})),
                ("oi_history", self._safe_endpoint("/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 26})),
                ("long_short", self._safe_endpoint("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": "1h", "limit": 26})),
                ("funding_history", self._safe_endpoint("/futures/data/fundingRate", {"symbol": symbol, "limit": 25})),
                ("basis_history", self._safe_endpoint("/futures/data/basis", {"pair": symbol, "contractType": "PERPETUAL", "period": "1h", "limit": 25})),
                ("taker_history", self._safe_endpoint("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": "15m", "limit": 16})),
                ("ticker_24h", self._safe_endpoint("/fapi/v1/ticker/24hr", {"symbol": symbol})),
            ]
            if key_configured:
                requests.extend([
                    ("top_accounts", self._safe_endpoint("/futures/data/topLongShortAccountRatio", {"symbol": symbol, "period": "1h", "limit": 25})),
                    ("top_positions", self._safe_endpoint("/futures/data/topLongShortPositionRatio", {"symbol": symbol, "period": "1h", "limit": 25})),
                ])
            results = await asyncio.gather(*(request for _, request in requests))
            data = {name: value for (name, _), value in zip(requests, results)}

            def number(value: Any) -> float | None:
                return _finite(value)

            def rows(name: str) -> list[dict[str, Any]]:
                raw_rows = data.get(name)
                return [dict(row) for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []

            def timestamp(row: dict[str, Any]) -> float | None:
                return number(row.get("timestamp", row.get("fundingTime", row.get("time"))))

            def history_coverage(raw_rows: list[dict[str, Any]]) -> float | None:
                stamps = [stamp for row in raw_rows if (stamp := timestamp(row)) is not None]
                return max(0.0, (max(stamps) - min(stamps)) / 3_600_000.0) if len(stamps) >= 2 else (0.0 if stamps else None)

            funding = data.get("funding") if isinstance(data.get("funding"), dict) else {}
            mark_price = number(funding.get("markPrice"))
            index_price = number(funding.get("indexPrice"))
            funding_rate = number(funding.get("lastFundingRate"))
            premium_pct = ((mark_price / index_price - 1.0) * 100.0) if mark_price is not None and index_price is not None and index_price > 0 else None
            next_funding_ms = number(funding.get("nextFundingTime"))

            current_oi = data.get("open_interest") if isinstance(data.get("open_interest"), dict) else {}
            oi_contracts = number(current_oi.get("openInterest"))
            oi_time = number(current_oi.get("time"))
            oi_rows = sorted(rows("oi_history"), key=lambda row: timestamp(row) if timestamp(row) is not None else -1)
            oi_coverage = history_coverage(oi_rows)
            oi_points: list[dict[str, Any]] = []
            for row in oi_rows:
                stamp = timestamp(row)
                contracts = number(row.get("sumOpenInterest"))
                notional = number(row.get("sumOpenInterestValue"))
                if notional is None and contracts is not None and mark_price is not None:
                    notional = contracts * mark_price
                if stamp is None:
                    continue
                oi_points.append({"timestamp": int(stamp), "time": _iso_from_ms(stamp), "value_usdt": notional, "open_interest": contracts})
            if oi_contracts is None and oi_rows:
                oi_contracts = number(oi_rows[-1].get("sumOpenInterest"))
                oi_time = timestamp(oi_rows[-1])
            open_interest_value = oi_contracts * mark_price if oi_contracts is not None and mark_price is not None else (
                number(current_oi.get("openInterestValue")) or (oi_points[-1].get("value_usdt") if oi_points else None)
            )
            oi_change_1h = None
            if len(oi_points) >= 2:
                latest_point = oi_points[-1]
                previous_point = max(oi_points[:-1], key=lambda point: point["timestamp"] if point["timestamp"] <= latest_point["timestamp"] - 3_000_000 else -1)
                gap_hours = (latest_point["timestamp"] - previous_point["timestamp"]) / 3_600_000.0
                if 0.5 <= gap_hours <= 1.5:
                    old_value = previous_point.get("value_usdt")
                    new_value = latest_point.get("value_usdt")
                    if old_value is not None and old_value > 0 and new_value is not None:
                        oi_change_1h = (new_value / old_value - 1.0) * 100.0
            oi_change_24h = None
            if oi_points and oi_coverage is not None and oi_coverage >= 23.0:
                latest_point = oi_points[-1]
                target_stamp = latest_point["timestamp"] - 24 * 3_600_000
                references = [point for point in oi_points if point["timestamp"] <= target_stamp]
                if references:
                    reference = references[-1]
                    old_value, new_value = reference.get("value_usdt"), latest_point.get("value_usdt")
                    if old_value is not None and old_value > 0 and new_value is not None:
                        oi_change_24h = (new_value / old_value - 1.0) * 100.0

            ls_rows = sorted(rows("long_short"), key=lambda row: timestamp(row) if timestamp(row) is not None else -1)
            latest_ls = ls_rows[-1] if ls_rows else {}
            ls_ratio = number(latest_ls.get("longShortRatio"))
            long_account = number(latest_ls.get("longAccount"))
            short_account = number(latest_ls.get("shortAccount"))
            ls_long_pct = long_account * 100 if long_account is not None else (ls_ratio / (1 + ls_ratio) * 100 if ls_ratio is not None and ls_ratio >= 0 else None)
            ls_short_pct = short_account * 100 if short_account is not None else (100 - ls_long_pct if ls_long_pct is not None else None)
            ls_coverage = history_coverage(ls_rows)
            ls_reference = {}
            if ls_rows and ls_coverage is not None and ls_coverage >= 23.0:
                latest_stamp = timestamp(ls_rows[-1])
                eligible = [row for row in ls_rows if timestamp(row) is not None and latest_stamp is not None and timestamp(row) <= latest_stamp - 24 * 3_600_000]
                if eligible:
                    ls_reference = eligible[-1]
            ls_24_ratio = number(ls_reference.get("longShortRatio")) if ls_reference else None
            ls_24_long = number(ls_reference.get("longAccount")) if ls_reference else None
            ls_24_short = number(ls_reference.get("shortAccount")) if ls_reference else None
            if ls_24_long is not None:
                ls_24_long *= 100
            if ls_24_short is not None:
                ls_24_short *= 100
            if ls_24_long is None and ls_24_ratio is not None and ls_24_ratio >= 0:
                ls_24_long = ls_24_ratio / (1 + ls_24_ratio) * 100
            if ls_24_short is None and ls_24_long is not None:
                ls_24_short = 100 - ls_24_long
            ls_history = []
            for row in ls_rows:
                stamp = timestamp(row)
                ratio = number(row.get("longShortRatio"))
                lp = number(row.get("longAccount"))
                sp = number(row.get("shortAccount"))
                if stamp is None:
                    continue
                lp = lp * 100 if lp is not None else (ratio / (1 + ratio) * 100 if ratio is not None and ratio >= 0 else None)
                sp = sp * 100 if sp is not None else (100 - lp if lp is not None else None)
                ls_history.append({"timestamp": int(stamp), "time": _iso_from_ms(stamp), "ratio": ratio, "percent_long": lp, "percent_short": sp})

            def ratio_payload(name: str) -> dict[str, Any]:
                top_rows = sorted(rows(name), key=lambda row: timestamp(row) if timestamp(row) is not None else -1)
                latest = top_rows[-1] if top_rows else {}
                ratio = number(latest.get("longShortRatio"))
                lp = number(latest.get("longAccount"))
                sp = number(latest.get("shortAccount"))
                derived = False
                if lp is None and ratio is not None and ratio >= 0:
                    lp, derived = ratio / (1 + ratio), True
                if sp is None and lp is not None:
                    sp = 1 - lp
                lp_pct = lp * 100 if lp is not None else None
                sp_pct = sp * 100 if sp is not None else (100 - lp_pct if lp_pct is not None else None)
                hist = []
                for row in top_rows:
                    stamp = timestamp(row)
                    r = number(row.get("longShortRatio"))
                    l = number(row.get("longAccount"))
                    sh = number(row.get("shortAccount"))
                    if stamp is None:
                        continue
                    if l is None and r is not None and r >= 0:
                        l, derived = r / (1 + r), True
                    if sh is None and l is not None:
                        sh = 1 - l
                    hist.append({"timestamp": int(stamp), "time": _iso_from_ms(stamp), "ratio": r,
                                 "percent_long": l * 100 if l is not None else None,
                                 "percent_short": sh * 100 if sh is not None else None,
                                 "percentages_derived": derived})
                return {"ratio": ratio, "long": lp_pct, "short": sp_pct, "percentages_derived": derived,
                        "history": hist, "updated_at": _iso_from_ms(timestamp(latest)) if latest else None}

            top_accounts = ratio_payload("top_accounts") if key_configured else {"ratio": None, "long": None, "short": None, "percentages_derived": False, "history": [], "updated_at": None}
            top_positions = ratio_payload("top_positions") if key_configured else {"ratio": None, "long": None, "short": None, "percentages_derived": False, "history": [], "updated_at": None}

            funding_rows = sorted(rows("funding_history"), key=lambda row: timestamp(row) if timestamp(row) is not None else -1)
            funding_points = []
            for row in funding_rows:
                stamp = timestamp(row)
                rate = number(row.get("fundingRate"))
                if stamp is None:
                    continue
                funding_points.append({"timestamp": int(stamp), "time": _iso_from_ms(stamp), "funding_rate": rate,
                                       "funding_rate_pct": rate * 100 if rate is not None else None,
                                       "mark_price": number(row.get("markPrice"))})

            basis_rows = sorted(rows("basis_history"), key=lambda row: timestamp(row) if timestamp(row) is not None else -1)
            basis_points = []
            for row in basis_rows:
                stamp = timestamp(row)
                if stamp is None:
                    continue
                rate = number(row.get("basisRate"))
                annualized = number(row.get("annualizedBasisRate"))
                basis_points.append({"timestamp": int(stamp), "time": _iso_from_ms(stamp),
                                     "basis_rate_pct": rate * 100 if rate is not None else None,
                                     "annualized_basis_pct": annualized * 100 if annualized is not None else None,
                                     "basis_usdt": number(row.get("basis")),
                                     "futures_price": number(row.get("futuresPrice")),
                                     "index_price": number(row.get("indexPrice"))})
            latest_basis = basis_points[-1] if basis_points else {}
            basis_coverage = history_coverage(basis_rows)

            ticker = data.get("ticker_24h") if isinstance(data.get("ticker_24h"), dict) else {}
            price_change_24h = number(ticker.get("priceChangePercent"))
            taker_rows = sorted(rows("taker_history"), key=lambda row: timestamp(row) if timestamp(row) is not None else -1)
            taker_windows = []
            for minutes, label in ((15, "15m"), (60, "1h"), (240, "4h")):
                required = minutes // 15
                selected = taker_rows[-required:]
                buy_values = [number(row.get("buyVol")) for row in selected]
                sell_values = [number(row.get("sellVol")) for row in selected]
                buy_volume = sum(value for value in buy_values if value is not None) if any(value is not None for value in buy_values) else None
                sell_volume = sum(value for value in sell_values if value is not None) if any(value is not None for value in sell_values) else None
                total = (buy_volume or 0) + (sell_volume or 0)
                buy_pct = buy_volume / total * 100 if buy_volume is not None and sell_volume is not None and total > 0 else None
                stamps = [timestamp(row) for row in selected if timestamp(row) is not None]
                last_stamp = max(stamps) if stamps else None
                points = len(selected)
                status = "available" if points >= required else ("partial" if points else "unavailable")
                taker_windows.append({"label": label, "minutes": minutes, "buy_volume": buy_volume,
                                      "sell_volume": sell_volume,
                                      "delta_volume": (buy_volume - sell_volume) if buy_volume is not None and sell_volume is not None else None,
                                      "buy_sell_ratio": (buy_volume / sell_volume) if buy_volume is not None and sell_volume and sell_volume > 0 else None,
                                      "buy_pct": buy_pct, "sell_pct": (100 - buy_pct) if buy_pct is not None else None,
                                      "points": points, "required_points": required,
                                      "coverage_minutes": min(minutes, points * 15),
                                      "updated_at": _iso_from_ms(last_stamp),
                                      "age_seconds": max(0.0, now - last_stamp / 1000.0) if last_stamp is not None else None,
                                      "status": status})
            taker_history = []
            for row in taker_rows:
                stamp = timestamp(row)
                buy = number(row.get("buyVol")); sell = number(row.get("sellVol"))
                if stamp is None:
                    continue
                taker_history.append({"timestamp": int(stamp), "time": _iso_from_ms(stamp),
                                      "ratio": number(row.get("buySellRatio")), "value_usdt": None,
                                      "open_interest": None, "funding_rate": None,
                                      "buy_volume": buy, "sell_volume": sell})

            if all(value is None for value in (funding_rate, open_interest_value, ls_ratio, top_positions.get("ratio"))):
                raise AnalyticsUnavailable("DERIVATIVES_UNAVAILABLE")

            funding_latest_stamp = timestamp(funding_rows[-1]) if funding_rows else None
            basis_latest_stamp = timestamp(basis_rows[-1]) if basis_rows else None
            ls_latest_stamp = timestamp(ls_rows[-1]) if ls_rows else None
            oi_status = "unavailable" if not oi_points else ("available" if (oi_coverage or 0) >= 23 else "partial")
            source = "binance-futures"
            sources = {
                "funding": {"status": "available" if funding_rate is not None else "unavailable", "updated_at": _iso_from_ms(number(funding.get("time")) or now * 1000), "age_seconds": None, "points": 1, "source": source},
                "open_interest_history": {"status": oi_status, "updated_at": _iso_from_ms(oi_points[-1]["timestamp"]) if oi_points else None, "age_seconds": max(0.0, now - oi_points[-1]["timestamp"] / 1000.0) if oi_points else None, "points": len(oi_points), "coverage_hours": oi_coverage, "source": source},
                "long_short_ratio": {"status": "unavailable" if not ls_rows else ("available" if (ls_coverage or 0) >= 23 else "partial"), "updated_at": _iso_from_ms(ls_latest_stamp), "age_seconds": max(0.0, now - ls_latest_stamp / 1000.0) if ls_latest_stamp else None, "points": len(ls_rows), "coverage_hours": ls_coverage, "source": source},
                "funding_history": {"status": "available" if funding_points else "unavailable", "updated_at": _iso_from_ms(funding_latest_stamp), "age_seconds": max(0.0, now - funding_latest_stamp / 1000.0) if funding_latest_stamp else None, "points": len(funding_points), "source": source},
                "basis": {"status": "available" if basis_points else "unavailable", "updated_at": _iso_from_ms(basis_latest_stamp), "age_seconds": max(0.0, now - basis_latest_stamp / 1000.0) if basis_latest_stamp else None, "points": len(basis_points), "coverage_hours": basis_coverage, "source": source},
                "taker_flow": {"status": "available" if taker_rows else "unavailable", "updated_at": _iso_from_ms(timestamp(taker_rows[-1]) if taker_rows else None), "points": len(taker_rows), "source": source},
                "top_trader_accounts": {"status": "api_key_missing" if not key_configured else ("available" if top_accounts["history"] else "unavailable"), "updated_at": top_accounts.get("updated_at"), "points": len(top_accounts["history"]), "source": source},
                "top_trader_positions": {"status": "api_key_missing" if not key_configured else ("available" if top_positions["history"] else "unavailable"), "updated_at": top_positions.get("updated_at"), "points": len(top_positions["history"]), "source": source},
            }
            factors = []
            if funding_rate is not None:
                factors.append(f"Funding {funding_rate * 100:.4f}% por período.")
            if oi_change_24h is not None:
                factors.append(f"El interés abierto cambió {oi_change_24h:+.2f}% en 24 horas.")
            if ls_long_pct is not None:
                factors.append(f"Las cuentas globales mantienen {ls_long_pct:.1f}% long y {ls_short_pct:.1f}% short.")
            if taker_windows and taker_windows[-1]["buy_pct"] is not None:
                factors.append(f"El flujo taker de 4H registra {taker_windows[-1]['buy_pct']:.1f}% de compras.")
            directions = []
            if funding_rate is not None and funding_rate > 0.0001: directions.append("comprador")
            if funding_rate is not None and funding_rate < -0.0001: directions.append("vendedor")
            if ls_long_pct is not None and ls_long_pct > 55: directions.append("comprador")
            elif ls_long_pct is not None and ls_long_pct < 45: directions.append("vendedor")
            if taker_windows and taker_windows[-1]["buy_pct"] is not None:
                if taker_windows[-1]["buy_pct"] >= 55: directions.append("comprador")
                elif taker_windows[-1]["buy_pct"] <= 45: directions.append("vendedor")
            bias = "insuficiente" if not directions else (directions[0] if len(set(directions)) == 1 else "mixto")
            market_reading = {"bias": bias, "summary": " ".join(factors) if factors else "Las fuentes no ofrecen suficientes métricas simultáneas para una lectura combinada.", "factors": factors, "factor_count": len(factors)}
            raw = {"funding": funding_rate, "next_funding_ms": next_funding_ms, "premium_pct": premium_pct,
                   "oi_value": open_interest_value, "oi_change_1h": oi_change_1h, "oi_change_24h": oi_change_24h,
                   "ls_ratio": ls_ratio, "ls_long_pct": ls_long_pct, "ls_ratio_24h": ls_24_ratio,
                   "top_ratio": top_positions.get("ratio")}
            output = {
                "symbol": symbol, "funding_rate": funding_rate, "funding_next": _iso_from_ms(next_funding_ms),
                "funding_updated_at": _iso_from_ms(number(funding.get("time")) or now * 1000),
                "funding_history": funding_points, "basis_history": basis_points,
                "basis_rate_pct": latest_basis.get("basis_rate_pct"),
                "annualized_basis_pct": latest_basis.get("annualized_basis_pct"),
                "basis_observed_at": _iso_from_ms(basis_latest_stamp), "basis_coverage_hours": basis_coverage,
                "funding_interval_hours": 8.0,
                "funding_annualized_pct": funding_rate * 3 * 365 * 100 if funding_rate is not None else None,
                "mark_price": mark_price, "index_price": index_price,
                "basis_usdt": latest_basis.get("basis_usdt"), "mark_index_delta": premium_pct,
                "price_change_24h_pct": price_change_24h,
                "open_interest": open_interest_value, "open_interest_contracts": oi_contracts,
                "open_interest_updated_at": _iso_from_ms(oi_time or (oi_points[-1]["timestamp"] if oi_points else None)),
                "oi_change_1h": oi_change_1h, "oi_change_24h": oi_change_24h,
                "oi_history": oi_points, "oi_history_latest_at": _iso_from_ms(oi_points[-1]["timestamp"]) if oi_points else None,
                "oi_history_coverage_hours": oi_coverage,
                "long_short_ratio": {"ratio": ls_ratio, "percent_long": ls_long_pct, "percent_short": ls_short_pct,
                                     "ratio_24h_ago": ls_24_ratio, "percent_long_24h_ago": ls_24_long,
                                     "percent_short_24h_ago": ls_24_short, "value_24h_ago": ls_24_ratio,
                                     "history": ls_history, "updated_at": _iso_from_ms(ls_latest_stamp), "coverage_hours": ls_coverage},
                "top_trader_accounts": top_accounts, "top_trader_positions": top_positions,
                "top_trader_ratio": {"ratio": top_positions.get("ratio"), "long": top_positions.get("long"), "short": top_positions.get("short")},
                "taker_flow": {"windows": taker_windows, "history": taker_history},
                "market_reading": market_reading, "sources": sources,
                "reading": " ".join(derivatives_engine.interpret(raw)), "updated_at": utc_iso(now), "raw": raw,
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
            now_epoch = self.clock()
            closed_candles = [candle for candle in candles if candle.get("close_t", float("inf")) <= now_epoch]
            last_closed = max((candle.get("close_t") for candle in closed_candles if candle.get("close_t") is not None), default=None)
            baseline_required = (240 // flow_engine.CANDLE_MINUTES) * 4
            coverage_complete = len(closed_candles) >= baseline_required
            quality_message = None if coverage_complete else (
                f"Historial parcial: {len(closed_candles)} velas cerradas; se requieren {baseline_required} para comparar el volumen de 4H."
            )
            output = {
                "symbol": symbol,
                "windows": windows,
                "cvd": _cvd_candles(candles, computed["cvd"], "UTC"),
                "volume_ratio": next((row.get("volume_ratio") for row in windows if row["label"] == "15m"), None),
                "reading": " ".join(flow_engine.interpret_flow(computed)),
                "updated_at": utc_iso(now_epoch),
                "source": "binance_futures_klines_5m",
                "status": "ok" if coverage_complete else "collecting",
                "quality": {
                    "history_candles": len(candles),
                    "closed_candles": len(closed_candles),
                    "history_minutes": len(closed_candles) * flow_engine.CANDLE_MINUTES,
                    "history_hours": round(len(closed_candles) * flow_engine.CANDLE_MINUTES / 60.0, 3),
                    "required_candles_for_4h_volume_baseline": baseline_required,
                    "volume_ratio_4h_ready": coverage_complete,
                    "last_closed_candle_at": utc_iso(last_closed) if last_closed is not None else None,
                    "last_closed_age_seconds": max(0.0, now_epoch - last_closed) if last_closed is not None else None,
                    "coverage_complete": coverage_complete,
                    "message": quality_message,
                },
                "price_cvd": flow_engine.price_cvd_context(candles, now=now_epoch),
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

    async def order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        """Fetch and cache a real USD-M Futures depth snapshot; never synthesize levels."""
        symbol = _validated_symbol(symbol)
        if limit not in (5, 10, 20, 50, 100):
            raise ValueError("INVALID_ORDER_BOOK_LIMIT")
        key = f"market:orderbook:v1:{symbol}:{limit}"
        cached = await self.cache.get_json(key)
        if isinstance(cached, dict) and cached.get("symbol") == symbol:
            return cached

        async with self._lock_for(self._flow_locks, f"orderbook:{symbol}:{limit}"):
            cached = await self.cache.get_json(key)
            if isinstance(cached, dict) and cached.get("symbol") == symbol:
                return cached
            raw = await self._safe_endpoint("/fapi/v1/depth", {"symbol": symbol, "limit": limit})
            if not isinstance(raw, dict):
                raise AnalyticsUnavailable("ORDER_BOOK_UNAVAILABLE")

            def levels(value: Any, *, reverse: bool) -> list[dict[str, float]]:
                result: list[dict[str, float]] = []
                if not isinstance(value, list):
                    return result
                for item in value:
                    if not isinstance(item, (list, tuple)) or len(item) < 2:
                        continue
                    price = _finite(item[0]); quantity = _finite(item[1])
                    if price is None or quantity is None or price <= 0 or quantity < 0:
                        continue
                    result.append({"price": price, "quantity": quantity, "notional_usdt": price * quantity})
                result.sort(key=lambda row: row["price"], reverse=reverse)
                return result[:limit]

            bids = levels(raw.get("bids"), reverse=True)
            asks = levels(raw.get("asks"), reverse=False)
            if not bids or not asks:
                raise AnalyticsUnavailable("ORDER_BOOK_UNAVAILABLE")
            best_bid, best_ask = bids[0]["price"], asks[0]["price"]
            if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
                raise AnalyticsUnavailable("ORDER_BOOK_INVALID_SNAPSHOT")
            spread = best_ask - best_bid
            mid = (best_ask + best_bid) / 2.0
            bid_depth = sum(row["notional_usdt"] for row in bids)
            ask_depth = sum(row["notional_usdt"] for row in asks)
            total_depth = bid_depth + ask_depth
            now = utc_iso(self.clock())
            result = {
                "symbol": symbol,
                "limit": limit,
                "last_update_id": int(_finite(raw.get("lastUpdateId")) or 0),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid_price": mid,
                "spread": spread,
                "spread_pct": spread / mid * 100.0 if mid > 0 else None,
                "bid_depth_usdt": bid_depth,
                "ask_depth_usdt": ask_depth,
                "bid_share_pct": bid_depth / total_depth * 100.0 if total_depth > 0 else None,
                "imbalance_pct": (bid_depth - ask_depth) / total_depth * 100.0 if total_depth > 0 else None,
                "bids": bids,
                "asks": asks,
                "levels_returned": min(len(bids), len(asks)),
                "source": "binance-futures-order-book",
                "status": "available",
                "updated_at": now,
            }
            await self.cache.set_json(key, result, ttl_seconds=ORDERBOOK_TTL)
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
