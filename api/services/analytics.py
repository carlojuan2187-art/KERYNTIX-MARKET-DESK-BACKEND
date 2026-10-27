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

DERIVATIVES_TTL = 30
FLOW_TTL = 10
ORDER_BOOK_TTL = 5
ORDER_BOOK_LEVELS = 20
REGIME_TTL = 90
CALENDAR_TTL = 300
DERIVATIVES_KEY_PREFIX = "market:derivatives:v1:"
FLOW_KEY_PREFIX = "market:flow:v1:"
ORDER_BOOK_KEY_PREFIX = "market:orderbook:v1:"
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


def _cvd_candles(candles: list[dict[str, float]], cvd_values: list[float], tz_name: str,
                 now: float | None = None) -> list[dict[str, Any]]:
    if not candles or not cvd_values:
        return []
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        zone = ZoneInfo("UTC")
    current = time.time() if now is None else now
    closed = [row for row in candles if row.get("close_t", float("inf")) <= current]
    tail = closed[-(240 // flow_engine.CANDLE_MINUTES):]
    points = min(len(cvd_values), len(tail))
    cvd_values = cvd_values[-points:]
    tail = tail[-points:]
    if points == 0:
        return []
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
        self._order_book_locks: dict[str, asyncio.Lock] = {}
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
        """Return current + historical derivatives data with per-source quality metadata.

        Historical market statistics are not tick streams. Each metric carries its
        source timestamp/coverage so the UI can distinguish a recent snapshot from
        an old or incomplete sample.
        """
        symbol = _validated_symbol(symbol)
        key = DERIVATIVES_KEY_PREFIX + symbol
        cached = await self.cache.get_json(key)
        if isinstance(cached, dict) and cached.get("symbol") == symbol:
            return cached

        async with self._lock_for(self._derivative_locks, symbol):
            cached = await self.cache.get_json(key)
            if isinstance(cached, dict) and cached.get("symbol") == symbol:
                return cached

            has_api_key = bool(getattr(self.rest, "api_key_configured", True))

            async def no_data() -> None:
                return None

            requests = [
                self._safe_endpoint("/fapi/v1/premiumIndex", {"symbol": symbol}),
                self._safe_endpoint("/fapi/v1/openInterest", {"symbol": symbol}),
                self._safe_endpoint("/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 26}),
                self._safe_endpoint("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": "1h", "limit": 26}),
                self._safe_endpoint("/fapi/v1/fundingRate", {"symbol": symbol, "limit": 25}),
                self._safe_endpoint("/futures/data/basis", {"pair": symbol, "contractType": "PERPETUAL", "period": "1h", "limit": 25}),
                self._safe_endpoint("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": "15m", "limit": 16}),
                self._safe_endpoint("/fapi/v1/ticker/24hr", {"symbol": symbol}),
                self._safe_endpoint("/futures/data/topLongShortAccountRatio", {"symbol": symbol, "period": "1h", "limit": 25}) if has_api_key else no_data(),
                self._safe_endpoint("/futures/data/topLongShortPositionRatio", {"symbol": symbol, "period": "1h", "limit": 25}) if has_api_key else no_data(),
            ]
            (premium, current_oi, oi_rows, ls_rows, funding_rows, basis_rows, taker_rows,
             ticker, top_account_rows, top_position_rows) = await asyncio.gather(*requests)

            now = self.clock()
            now_ms = int(now * 1000)
            f = derivatives_engine._to_float

            def rows_with_values(rows: Any, value_key: str, timestamp_key: str = "timestamp") -> list[dict[str, Any]]:
                result: list[dict[str, Any]] = []
                if not isinstance(rows, list):
                    return result
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    value = f(row.get(value_key))
                    timestamp = f(row.get(timestamp_key, row.get("time", row.get("fundingTime"))))
                    if value is None or timestamp is None or timestamp <= 0:
                        continue
                    result.append({"timestamp": int(timestamp), "value": value})
                return sorted(result, key=lambda item: item["timestamp"])

            def source_meta(timestamp_ms: Any, *, status: str, points: int | None = None,
                            coverage_hours: float | None = None, source: str | None = None) -> dict[str, Any]:
                stamp = f(timestamp_ms)
                age = max(0.0, now - stamp / 1000.0) if stamp is not None and stamp > 0 else None
                return {
                    "status": status,
                    "updated_at": _iso_from_ms(stamp),
                    "age_seconds": round(age, 1) if age is not None else None,
                    "points": points,
                    "coverage_hours": round(coverage_hours, 2) if coverage_hours is not None else None,
                    "source": source,
                }

            def percent_pair(row: dict[str, Any] | None, ratio_key: str = "longShortRatio") -> tuple[float | None, float | None, float | None, bool]:
                if not row:
                    return None, None, None, False
                ratio = f(row.get(ratio_key))
                long_raw = f(row.get("longAccount", row.get("longPosition")))
                short_raw = f(row.get("shortAccount", row.get("shortPosition")))
                derived = False
                if long_raw is not None and 0 <= long_raw <= 1:
                    long_pct = long_raw * 100.0
                elif ratio is not None and ratio >= 0:
                    long_pct = ratio / (1.0 + ratio) * 100.0
                    derived = True
                else:
                    long_pct = None
                if short_raw is not None and 0 <= short_raw <= 1:
                    short_pct = short_raw * 100.0
                elif long_pct is not None:
                    short_pct = 100.0 - long_pct
                    derived = True
                else:
                    short_pct = None
                return ratio, long_pct, short_pct, derived

            raw: dict[str, Any] = {
                "symbol": symbol, "funding": None, "next_funding_ms": None,
                "mark_price": None, "index_price": None, "premium_pct": None,
                "oi_value": None, "oi_contracts": None,
                "oi_hist_latest_value": None, "oi_change_1h": None, "oi_change_24h": None,
                "ls_ratio": None, "ls_long_pct": None, "ls_short_pct": None,
                "ls_ratio_24h": None, "ls_long_pct_24h": None, "ls_short_pct_24h": None,
                "price_change_24h_pct": None,
            }
            if isinstance(premium, dict):
                raw["funding"] = f(premium.get("lastFundingRate"))
                raw["next_funding_ms"] = f(premium.get("nextFundingTime"))
                raw["mark_price"] = f(premium.get("markPrice"))
                raw["index_price"] = f(premium.get("indexPrice"))
                if raw["mark_price"] is not None and raw["index_price"] is not None and raw["index_price"] > 0:
                    raw["premium_pct"] = (raw["mark_price"] / raw["index_price"] - 1.0) * 100.0

            current_oi_at = None
            if isinstance(current_oi, dict):
                contracts = f(current_oi.get("openInterest"))
                current_oi_at = f(current_oi.get("time"))
                raw["oi_contracts"] = contracts
                if contracts is not None and contracts >= 0 and raw["mark_price"] is not None:
                    raw["oi_value"] = contracts * raw["mark_price"]

            oi_points: list[dict[str, Any]] = []
            if isinstance(oi_rows, list):
                for row in oi_rows:
                    if not isinstance(row, dict):
                        continue
                    value = f(row.get("sumOpenInterestValue"))
                    timestamp = f(row.get("timestamp"))
                    quantity = f(row.get("sumOpenInterest"))
                    if value is None or timestamp is None or timestamp <= 0:
                        continue
                    oi_points.append({"timestamp": int(timestamp), "time": _iso_from_ms(timestamp),
                                      "value_usdt": value, "open_interest": quantity})
            oi_points.sort(key=lambda row: row["timestamp"])
            oi_coverage_hours = ((oi_points[-1]["timestamp"] - oi_points[0]["timestamp"]) / 3_600_000) if len(oi_points) >= 2 else 0.0
            if oi_points:
                raw["oi_hist_latest_value"] = oi_points[-1]["value_usdt"]
                last_point = oi_points[-1]
                last_ts = last_point["timestamp"]
                # Only compute deltas against temporally aligned historical observations.
                previous_1h = next((row for row in reversed(oi_points[:-1]) if row["timestamp"] <= last_ts - 3_300_000), None)
                previous_24h = next((row for row in reversed(oi_points[:-1]) if row["timestamp"] <= last_ts - 23 * 3_600_000), None)
                latest_age = max(0.0, now - last_ts / 1000.0)
                if previous_1h and latest_age <= 2 * 3600:
                    gap_hours = (last_ts - previous_1h["timestamp"]) / 3_600_000
                    if 0.8 <= gap_hours <= 1.5:
                        raw["oi_change_1h"] = derivatives_engine._pct_change(last_point["value_usdt"], previous_1h["value_usdt"])
                if previous_24h and oi_coverage_hours >= 23 and latest_age <= 2 * 3600:
                    gap_hours = (last_ts - previous_24h["timestamp"]) / 3_600_000
                    if 23 <= gap_hours <= 25.5:
                        raw["oi_change_24h"] = derivatives_engine._pct_change(last_point["value_usdt"], previous_24h["value_usdt"])

            ls_points: list[dict[str, Any]] = []
            if isinstance(ls_rows, list):
                for row in ls_rows:
                    if not isinstance(row, dict):
                        continue
                    timestamp = f(row.get("timestamp"))
                    ratio = f(row.get("longShortRatio"))
                    long_raw = f(row.get("longAccount"))
                    short_raw = f(row.get("shortAccount"))
                    if timestamp is None or timestamp <= 0 or ratio is None:
                        continue
                    if long_raw is None and ratio >= 0:
                        long_raw = ratio / (1.0 + ratio)
                    if short_raw is None and long_raw is not None:
                        short_raw = 1.0 - long_raw
                    ls_points.append({"timestamp": int(timestamp), "time": _iso_from_ms(timestamp),
                                      "ratio": ratio,
                                      "percent_long": long_raw * 100.0 if long_raw is not None and 0 <= long_raw <= 1 else None,
                                      "percent_short": short_raw * 100.0 if short_raw is not None and 0 <= short_raw <= 1 else None})
            ls_points.sort(key=lambda row: row["timestamp"])
            if ls_points:
                current_ls = ls_points[-1]
                raw["ls_ratio"] = current_ls["ratio"]
                raw["ls_long_pct"] = current_ls["percent_long"]
                raw["ls_short_pct"] = current_ls["percent_short"]
                ls_coverage = (ls_points[-1]["timestamp"] - ls_points[0]["timestamp"]) / 3_600_000 if len(ls_points) >= 2 else 0.0
                if ls_coverage >= 23:
                    target = ls_points[-1]["timestamp"] - 24 * 3_600_000
                    historic = min(ls_points, key=lambda row: abs(row["timestamp"] - target))
                    if abs(historic["timestamp"] - target) <= 90 * 60_000:
                        raw["ls_ratio_24h"] = historic["ratio"]
                        raw["ls_long_pct_24h"] = historic["percent_long"]
                        raw["ls_short_pct_24h"] = historic["percent_short"]
            else:
                ls_coverage = 0.0

            funding_history: list[dict[str, Any]] = []
            if isinstance(funding_rows, list):
                for row in funding_rows:
                    if not isinstance(row, dict):
                        continue
                    rate = f(row.get("fundingRate"))
                    stamp = f(row.get("fundingTime"))
                    if rate is None or stamp is None or stamp <= 0:
                        continue
                    funding_history.append({"timestamp": int(stamp), "time": _iso_from_ms(stamp),
                                            "funding_rate": rate, "funding_rate_pct": rate * 100.0,
                                            "mark_price": f(row.get("markPrice"))})
            funding_history.sort(key=lambda row: row["timestamp"])
            # Settlement intervals can differ by symbol; estimate annualized funding from
            # observed timestamps instead of assuming every contract settles every 8 hours.
            intervals = [(funding_history[i]["timestamp"] - funding_history[i - 1]["timestamp"]) / 3_600_000
                         for i in range(1, len(funding_history))
                         if funding_history[i]["timestamp"] > funding_history[i - 1]["timestamp"]]
            average_interval_hours = sum(intervals) / len(intervals) if intervals else None
            funding_annualized_pct = None
            if raw["funding"] is not None and average_interval_hours and average_interval_hours >= 0.25:
                funding_annualized_pct = raw["funding"] * (365.25 * 24.0 / average_interval_hours) * 100.0

            # Contract basis is distinct from mark/index premium. Binance provides
            # basisRate and annualizedBasisRate as separate fields on its basis endpoint.
            basis_history: list[dict[str, Any]] = []
            if isinstance(basis_rows, list):
                for row in basis_rows:
                    if not isinstance(row, dict):
                        continue
                    stamp = f(row.get("timestamp"))
                    basis_rate = f(row.get("basisRate"))
                    annualized_basis_rate = f(row.get("annualizedBasisRate"))
                    basis_value = f(row.get("basis"))
                    futures_price = f(row.get("futuresPrice"))
                    index_price = f(row.get("indexPrice"))
                    if stamp is None or stamp <= 0:
                        continue
                    basis_history.append({
                        "timestamp": int(stamp), "time": _iso_from_ms(stamp),
                        "basis_rate_pct": basis_rate * 100.0 if basis_rate is not None else None,
                        "annualized_basis_pct": annualized_basis_rate * 100.0 if annualized_basis_rate is not None else None,
                        "basis_usdt": basis_value,
                        "futures_price": futures_price,
                        "index_price": index_price,
                    })
            basis_history.sort(key=lambda row: row["timestamp"])
            basis_latest = basis_history[-1] if basis_history else None
            basis_age_ms = now_ms - basis_latest["timestamp"] if basis_latest else None
            basis_status = (
                "unavailable" if not basis_latest else
                "stale" if basis_age_ms is not None and basis_age_ms > 3 * 3_600_000 else
                "available" if len(basis_history) >= 2 else "partial"
            )

            taker_rows_norm: list[dict[str, Any]] = []
            if isinstance(taker_rows, list):
                for row in taker_rows:
                    if not isinstance(row, dict):
                        continue
                    buy_vol, sell_vol = f(row.get("buyVol")), f(row.get("sellVol"))
                    stamp = f(row.get("timestamp"))
                    if buy_vol is None or sell_vol is None or stamp is None or stamp <= 0 or buy_vol < 0 or sell_vol < 0:
                        continue
                    total = buy_vol + sell_vol
                    taker_rows_norm.append({"timestamp": int(stamp), "time": _iso_from_ms(stamp),
                                            "buy_volume": buy_vol, "sell_volume": sell_vol,
                                            "buy_sell_ratio": buy_vol / sell_vol if sell_vol > 0 else None,
                                            "buy_pct": buy_vol / total * 100.0 if total > 0 else None,
                                            "sell_pct": sell_vol / total * 100.0 if total > 0 else None,
                                            "delta_volume": buy_vol - sell_vol})
            taker_rows_norm.sort(key=lambda row: row["timestamp"])
            taker_windows = []
            for label, minutes, count in (("15m", 15, 1), ("1h", 60, 4), ("4h", 240, 16)):
                sample = taker_rows_norm[-count:]
                buy_volume = sum(row["buy_volume"] for row in sample)
                sell_volume = sum(row["sell_volume"] for row in sample)
                total_volume = buy_volume + sell_volume
                coverage = len(sample) * 15
                latest_stamp = sample[-1]["timestamp"] if sample else None
                age = max(0.0, now - latest_stamp / 1000.0) if latest_stamp else None
                ready = bool(sample) and (count == 1 or len(sample) >= count) and age is not None and age <= max(45 * 60, int(minutes * 60 * 1.5))
                taker_windows.append({
                    "label": label, "minutes": minutes, "buy_volume": buy_volume if sample else None,
                    "sell_volume": sell_volume if sample else None,
                    "delta_volume": buy_volume - sell_volume if sample else None,
                    "buy_sell_ratio": buy_volume / sell_volume if sample and sell_volume > 0 else None,
                    "buy_pct": buy_volume / total_volume * 100.0 if sample and total_volume > 0 else None,
                    "sell_pct": sell_volume / total_volume * 100.0 if sample and total_volume > 0 else None,
                    "points": len(sample), "required_points": count,
                    "coverage_minutes": coverage, "updated_at": _iso_from_ms(latest_stamp),
                    "age_seconds": round(age, 1) if age is not None else None,
                    "status": "available" if ready else ("partial" if sample else "unavailable"),
                })

            def top_series(rows: Any) -> list[dict[str, Any]]:
                result: list[dict[str, Any]] = []
                if not isinstance(rows, list):
                    return result
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    stamp = f(row.get("timestamp"))
                    ratio, long_pct, short_pct, derived = percent_pair(row)
                    if stamp is None or stamp <= 0:
                        continue
                    result.append({"timestamp": int(stamp), "time": _iso_from_ms(stamp), "ratio": ratio,
                                   "percent_long": long_pct, "percent_short": short_pct,
                                   "percentages_derived": derived})
                return sorted(result, key=lambda row: row["timestamp"])

            top_accounts = top_series(top_account_rows)
            top_positions = top_series(top_position_rows)
            if isinstance(ticker, dict):
                raw["price_change_24h_pct"] = f(ticker.get("priceChangePercent"))

            funding_stamp = f(premium.get("time")) if isinstance(premium, dict) else None
            if funding_stamp is None:
                funding_stamp = now_ms if isinstance(premium, dict) else None
            current_oi_age_ms = now_ms - current_oi_at if current_oi_at is not None else None
            oi_current_status = (
                "unavailable" if raw["oi_value"] is None else
                "stale" if current_oi_age_ms is not None and current_oi_age_ms > 120_000 else "available"
            )
            oi_hist_age_ms = now_ms - oi_points[-1]["timestamp"] if oi_points else None
            oi_hist_status = (
                "unavailable" if not oi_points else
                "stale" if oi_hist_age_ms is not None and oi_hist_age_ms > 2 * 3_600_000 else
                "available" if len(oi_points) >= 2 and oi_coverage_hours >= 23 else "partial"
            )
            ls_age_ms = now_ms - ls_points[-1]["timestamp"] if ls_points else None
            ls_status = "unavailable" if not ls_points else "stale" if ls_age_ms is not None and ls_age_ms > 3 * 3_600_000 else "available"
            funding_history_status = "available" if len(funding_history) >= 2 else ("partial" if funding_history else "unavailable")
            taker_status = "available" if any(row["status"] == "available" for row in taker_windows) else ("partial" if taker_rows_norm else "unavailable")
            top_acc_age_ms = now_ms - top_accounts[-1]["timestamp"] if top_accounts else None
            top_pos_age_ms = now_ms - top_positions[-1]["timestamp"] if top_positions else None
            top_account_status = "api_key_missing" if not has_api_key else ("unavailable" if not top_accounts else "stale" if top_acc_age_ms is not None and top_acc_age_ms > 3 * 3_600_000 else "available")
            top_position_status = "api_key_missing" if not has_api_key else ("unavailable" if not top_positions else "stale" if top_pos_age_ms is not None and top_pos_age_ms > 3 * 3_600_000 else "available")

            updated_at = utc_iso(now)
            oi_latest_hist_at = oi_points[-1]["timestamp"] if oi_points else None
            ls_latest_at = ls_points[-1]["timestamp"] if ls_points else None
            top_accounts_latest = top_accounts[-1] if top_accounts else None
            top_positions_latest = top_positions[-1] if top_positions else None
            top_acc_ratio = top_accounts_latest.get("ratio") if top_accounts_latest else None
            top_acc_long = top_accounts_latest.get("percent_long") if top_accounts_latest else None
            top_acc_short = top_accounts_latest.get("percent_short") if top_accounts_latest else None
            top_acc_derived = bool(top_accounts_latest.get("percentages_derived")) if top_accounts_latest else False
            top_pos_ratio = top_positions_latest.get("ratio") if top_positions_latest else None
            top_pos_long = top_positions_latest.get("percent_long") if top_positions_latest else None
            top_pos_short = top_positions_latest.get("percent_short") if top_positions_latest else None
            top_pos_derived = bool(top_positions_latest.get("percentages_derived")) if top_positions_latest else False

            raw["oi_change_1h"] = raw["oi_change_1h"] if oi_hist_status != "unavailable" else None
            raw["oi_change_24h"] = raw["oi_change_24h"] if oi_hist_status == "available" else None
            # Derivatives engine keeps its original crowding thresholds. Taker flow is added
            # as additional evidence separately and never presented as a trade guarantee.
            legacy_reading = " ".join(derivatives_engine.interpret(raw))
            price_change = raw["price_change_24h_pct"]
            taker_1h = next((row for row in taker_windows if row["label"] == "1h"), {})
            evidence: list[str] = []
            buyer_score = 0
            seller_score = 0
            if taker_1h.get("buy_pct") is not None and taker_1h.get("status") == "available":
                if taker_1h["buy_pct"] >= 55:
                    buyer_score += 1
                    evidence.append(f"Compra agresiva dominante en 1H ({taker_1h['buy_pct']:.1f}% del volumen taker).")
                elif taker_1h["buy_pct"] <= 45:
                    seller_score += 1
                    evidence.append(f"Venta agresiva dominante en 1H ({taker_1h['sell_pct']:.1f}% del volumen taker).")
                else:
                    evidence.append("El flujo taker de 1H está relativamente equilibrado.")
            if price_change is not None and raw["oi_change_24h"] is not None:
                if price_change >= 1 and raw["oi_change_24h"] >= 1:
                    buyer_score += 1
                    evidence.append("Precio y OI suben en 24H: aumenta la exposición mientras el precio avanza; no identifica por sí solo el lado de las nuevas posiciones.")
                elif price_change <= -1 and raw["oi_change_24h"] >= 1:
                    seller_score += 1
                    evidence.append("Precio cae mientras aumenta el OI en 24H: aumenta la exposición durante el descenso; no confirma por sí solo quién tiene ventaja.")
                elif abs(raw["oi_change_24h"]) < 1:
                    evidence.append("El OI cambia poco en 24H; no hay una expansión clara de posiciones según esta ventana.")
            if raw["funding"] is not None and abs(raw["funding"]) >= 0.0005:
                evidence.append("Funding extremo frente al umbral interno: el posicionamiento puede estar cargado; es un indicador de coste y concentración, no una señal direccional aislada.")
            if raw["ls_ratio"] is not None:
                if raw["ls_ratio"] >= 1.5:
                    evidence.append("Hay más cuentas net-long que net-short en el último registro disponible.")
                elif raw["ls_ratio"] <= (1 / 1.5):
                    evidence.append("Hay más cuentas net-short que net-long en el último registro disponible.")
            bias = "comprador" if buyer_score > seller_score else "vendedor" if seller_score > buyer_score else "mixto"
            if len(evidence) < 2:
                bias = "insuficiente"
                summary = "Datos parciales: todavía no hay dos factores independientes suficientes para resumir el posicionamiento."
            elif bias == "comprador":
                summary = "Hay evidencia de presión compradora, pero debe confirmarse con el contexto de precio, OI y liquidez."
            elif bias == "vendedor":
                summary = "Hay evidencia de presión vendedora, pero debe confirmarse con el contexto de precio, OI y liquidez."
            else:
                summary = "Los factores disponibles no coinciden en una única dirección; el posicionamiento se considera mixto."

            output = {
                "symbol": symbol,
                "funding_rate": raw["funding"],
                "funding_next": _iso_from_ms(raw["next_funding_ms"]),
                "funding_updated_at": _iso_from_ms(funding_stamp),
                "funding_history": funding_history[-25:],
                "funding_interval_hours": round(average_interval_hours, 3) if average_interval_hours is not None else None,
                "funding_annualized_pct": funding_annualized_pct,
                "basis_history": basis_history[-25:],
                "basis_rate_pct": basis_latest.get("basis_rate_pct") if basis_latest else None,
                "annualized_basis_pct": basis_latest.get("annualized_basis_pct") if basis_latest else None,
                "basis_observed_at": basis_latest.get("time") if basis_latest else None,
                "basis_coverage_hours": round((basis_history[-1]["timestamp"] - basis_history[0]["timestamp"]) / 3_600_000, 2) if len(basis_history) >= 2 else 0.0,
                "mark_price": raw["mark_price"],
                "index_price": raw["index_price"],
                "basis_usdt": (raw["mark_price"] - raw["index_price"]) if raw["mark_price"] is not None and raw["index_price"] is not None else None,
                "mark_index_delta": raw["premium_pct"],
                "price_change_24h_pct": raw["price_change_24h_pct"],
                "open_interest": raw["oi_value"],
                "open_interest_contracts": raw["oi_contracts"],
                "open_interest_updated_at": _iso_from_ms(current_oi_at),
                "oi_change_1h": raw["oi_change_1h"],
                "oi_change_24h": raw["oi_change_24h"],
                "oi_history": oi_points[-26:],
                "oi_history_latest_at": _iso_from_ms(oi_latest_hist_at),
                "oi_history_coverage_hours": round(oi_coverage_hours, 2),
                "long_short_ratio": {
                    "ratio": raw["ls_ratio"], "percent_long": raw["ls_long_pct"], "percent_short": raw["ls_short_pct"],
                    "ratio_24h_ago": raw["ls_ratio_24h"], "percent_long_24h_ago": raw["ls_long_pct_24h"],
                    "percent_short_24h_ago": raw["ls_short_pct_24h"],
                    "value_24h_ago": raw["ls_ratio_24h"],
                    "history": ls_points[-26:],
                    "updated_at": _iso_from_ms(ls_latest_at),
                    "coverage_hours": round(ls_coverage, 2),
                },
                "top_trader_accounts": {
                    "ratio": top_acc_ratio, "long": top_acc_long, "short": top_acc_short,
                    "percentages_derived": top_acc_derived, "history": top_accounts[-25:],
                    "updated_at": top_accounts_latest.get("time") if top_accounts_latest else None,
                },
                "top_trader_positions": {
                    "ratio": top_pos_ratio, "long": top_pos_long, "short": top_pos_short,
                    "percentages_derived": top_pos_derived, "history": top_positions[-25:],
                    "updated_at": top_positions_latest.get("time") if top_positions_latest else None,
                },
                # Backward-compatible alias; the UI now labels this as positions, not accounts.
                "top_trader_ratio": {"long": top_pos_long, "short": top_pos_short, "ratio": top_pos_ratio},
                "taker_flow": {"windows": taker_windows, "history": taker_rows_norm[-16:]},
                "market_reading": {"bias": bias, "summary": summary, "factors": evidence, "factor_count": len(evidence)},
                "sources": {
                    "funding": source_meta(funding_stamp, status="available" if raw["funding"] is not None else "unavailable", source="/fapi/v1/premiumIndex"),
                    "open_interest_current": source_meta(current_oi_at, status=oi_current_status, source="/fapi/v1/openInterest"),
                    "open_interest_history": source_meta(oi_latest_hist_at, status=oi_hist_status, points=len(oi_points), coverage_hours=oi_coverage_hours, source="/futures/data/openInterestHist"),
                    "global_long_short": source_meta(ls_latest_at, status=ls_status, points=len(ls_points), coverage_hours=ls_coverage, source="/futures/data/globalLongShortAccountRatio"),
                    "funding_history": source_meta(funding_history[-1]["timestamp"] if funding_history else None, status=funding_history_status, points=len(funding_history), source="/fapi/v1/fundingRate"),
                    "basis": source_meta(basis_latest["timestamp"] if basis_latest else None, status=basis_status, points=len(basis_history), coverage_hours=((basis_history[-1]["timestamp"] - basis_history[0]["timestamp"]) / 3_600_000 if len(basis_history) >= 2 else 0), source="/futures/data/basis"),
                    "taker_flow": source_meta(taker_rows_norm[-1]["timestamp"] if taker_rows_norm else None, status=taker_status, points=len(taker_rows_norm), coverage_hours=(len(taker_rows_norm) * 0.25 if taker_rows_norm else 0), source="/futures/data/takerlongshortRatio"),
                    "top_trader_accounts": source_meta(top_accounts_latest.get("timestamp") if top_accounts_latest else None, status=top_account_status, points=len(top_accounts), coverage_hours=((top_accounts[-1]["timestamp"]-top_accounts[0]["timestamp"])/3_600_000 if len(top_accounts)>1 else 0), source="/futures/data/topLongShortAccountRatio"),
                    "top_trader_positions": source_meta(top_positions_latest.get("timestamp") if top_positions_latest else None, status=top_position_status, points=len(top_positions), coverage_hours=((top_positions[-1]["timestamp"]-top_positions[0]["timestamp"])/3_600_000 if len(top_positions)>1 else 0), source="/futures/data/topLongShortPositionRatio"),
                    "price_24h": source_meta(f(ticker.get("closeTime")) if isinstance(ticker, dict) else None, status="available" if raw["price_change_24h_pct"] is not None else "unavailable", source="/fapi/v1/ticker/24hr"),
                },
                "reading": legacy_reading,
                "updated_at": updated_at,
                "raw": raw,
            }
            if all(output.get(field) is None for field in ("funding_rate", "open_interest", "mark_index_delta", "long_short_ratio")):
                raise AnalyticsUnavailable("DERIVATIVES_UNAVAILABLE")
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
            now = self.clock()
            computed = flow_engine.compute_flow(symbol, candles, now=now)
            windows = []
            for row in computed["windows"]:
                windows.append({
                    "label": row["label"], "minutes": row["minutes"],
                    "buy_pct": row["buy_pct"], "delta": row["delta_base"],
                    "delta_usdt": row["delta_quote"], "price": row["price"],
                    "change_pct": row["price_chg"], "volume_ratio": row.get("volume_ratio"),
                    "volume_usdt": row["volume_quote"],
                })
            closed_candles = [row for row in candles if row.get("close_t", float("inf")) <= now]
            last_closed = max((row.get("close_t", 0.0) for row in closed_candles), default=None)
            last_closed_age = max(0.0, now - last_closed) if last_closed is not None else None
            required_for_4h_baseline = 4 * (240 // flow_engine.CANDLE_MINUTES)
            latest_window_4h = next((row for row in windows if row["label"] == "4h"), {})
            quality = {
                "history_candles": len(candles),
                "closed_candles": len(closed_candles),
                "history_minutes": len(closed_candles) * flow_engine.CANDLE_MINUTES,
                "history_hours": round(len(closed_candles) * flow_engine.CANDLE_MINUTES / 60.0, 2),
                "required_candles_for_4h_volume_baseline": required_for_4h_baseline,
                "volume_ratio_4h_ready": latest_window_4h.get("volume_ratio") is not None,
                "last_closed_candle_at": utc_iso(last_closed) if last_closed is not None else None,
                "last_closed_age_seconds": round(last_closed_age, 1) if last_closed_age is not None else None,
                "coverage_complete": len(closed_candles) >= required_for_4h_baseline,
            }
            if not quality["coverage_complete"]:
                quality["message"] = "Historial parcial: el ratio de volumen de 4H necesita aproximadamente 16 horas de velas cerradas."
            elif not quality["volume_ratio_4h_ready"]:
                quality["message"] = "Hay historial suficiente, pero no se pudo formar una referencia fiable de volumen para 4H."
            else:
                quality["message"] = None
            price_cvd = flow_engine.price_cvd_context(candles, now=now)
            output = {
                "symbol": symbol,
                "windows": windows,
                "cvd": _cvd_candles(candles, computed["cvd"], "UTC", now=now),
                "price_cvd": price_cvd,
                "quality": quality,
                "volume_ratio": next((row.get("volume_ratio") for row in windows if row["label"] == "15m"), None),
                "reading": " ".join(flow_engine.interpret_flow(computed)),
                "updated_at": utc_iso(now),
                "source": "binance_futures_klines_5m",
                "status": "ok",
                "raw": computed,
            }
            await self.cache.set_json(key, output, ttl_seconds=FLOW_TTL)
            return self._flow_for_timezone(output, tz_name)

    async def order_book(self, symbol: str, limit: int = ORDER_BOOK_LEVELS) -> dict[str, Any]:
        """Return a short-lived, real Binance Futures order-book snapshot."""
        symbol = _validated_symbol(symbol)
        if limit not in (5, 10, 20, 50, 100):
            raise ValueError("INVALID_ORDER_BOOK_LIMIT")
        key = f"{ORDER_BOOK_KEY_PREFIX}{symbol}:{limit}"
        cached = await self.cache.get_json(key)
        now = self.clock()
        if isinstance(cached, dict) and now - float(cached.get("updated_epoch", 0) or 0) < ORDER_BOOK_TTL:
            return cached
        async with self._lock_for(self._order_book_locks, key):
            cached = await self.cache.get_json(key)
            now = self.clock()
            if isinstance(cached, dict) and now - float(cached.get("updated_epoch", 0) or 0) < ORDER_BOOK_TTL:
                return cached
            raw = await self._safe_endpoint("/fapi/v1/depth", {"symbol": symbol, "limit": limit})
            if not isinstance(raw, dict):
                raise AnalyticsUnavailable("ORDER_BOOK_UNAVAILABLE")

            def levels(rows: Any, *, descending: bool) -> list[dict[str, float]]:
                output: list[dict[str, float]] = []
                if not isinstance(rows, list):
                    return output
                for row in rows:
                    if not isinstance(row, (list, tuple)) or len(row) < 2:
                        continue
                    price = _finite(row[0])
                    quantity = _finite(row[1])
                    if price is None or quantity is None or price <= 0 or quantity < 0:
                        continue
                    output.append({"price": price, "quantity": quantity, "notional_usdt": price * quantity})
                output.sort(key=lambda row: row["price"], reverse=descending)
                return output[:limit]

            bids = levels(raw.get("bids"), descending=True)
            asks = levels(raw.get("asks"), descending=False)
            if not bids or not asks:
                raise AnalyticsUnavailable("ORDER_BOOK_EMPTY")
            best_bid, best_ask = bids[0]["price"], asks[0]["price"]
            mid = (best_bid + best_ask) / 2.0
            bid_depth = sum(row["notional_usdt"] for row in bids)
            ask_depth = sum(row["notional_usdt"] for row in asks)
            combined_depth = bid_depth + ask_depth
            spread = max(0.0, best_ask - best_bid)
            output = {
                "symbol": symbol,
                "limit": limit,
                "last_update_id": int(raw.get("lastUpdateId") or 0),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid_price": mid,
                "spread": spread,
                "spread_pct": spread / mid * 100.0 if mid > 0 else None,
                "bid_depth_usdt": bid_depth,
                "ask_depth_usdt": ask_depth,
                "bid_share_pct": bid_depth / combined_depth * 100.0 if combined_depth > 0 else None,
                "imbalance_pct": (bid_depth - ask_depth) / combined_depth * 100.0 if combined_depth > 0 else None,
                "bids": bids,
                "asks": asks,
                "levels_returned": min(len(bids), len(asks)),
                "source": "binance-futures-order-book",
                "status": "ok",
                "updated_at": utc_iso(now),
                "updated_epoch": now,
            }
            await self.cache.set_json(key, output, ttl_seconds=ORDER_BOOK_TTL)
            return output

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
                raise AnalyticsUnavailable("REGIME_DATA_UNAVAILABLE")

            breadth = await self._breadth(frames.get("1h"))
            try:
                derivatives = await self.derivatives("BTCUSDT")
                raw_derivatives = derivatives.get("raw") or {}
            except Exception as exc:
                logger.info("Regime derivatives context unavailable: %s", exc)
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
