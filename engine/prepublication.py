"""Final anti-late publication gate mirrored from app.signal_lifecycle.py.

A candidate is only stored if its entry zone has not already been touched and
no one-minute candle since the strategy confirmation invalidated it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from engine.liquidity_strategy import MAX_PREENTRY_DISTANCE_ATR, MIN_PREENTRY_DISTANCE_ATR

STATUS_TOUCHED = "TOUCHED"
STATUS_CANCELLED = "CANCELLED"
CANCEL_TP_REACHED_BEFORE_ENTRY = "TP_REACHED_BEFORE_ENTRY"
CANCEL_INVALID_GEOMETRY = "INVALID_GEOMETRY"
CANCEL_AMBIGUOUS_INTRABAR = "AMBIGUOUS_ENTRY_TP_ORDER"
CANCEL_PRE_ENTRY_INVALIDATED = "PRE_ENTRY_INVALIDATED"
CANCEL_AMBIGUOUS_ENTRY_SL = "AMBIGUOUS_ENTRY_SL_ORDER"


def _utc_naive(value: Any) -> datetime | None:
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _price(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _entry_zone(signal: dict[str, Any]) -> tuple[float, float]:
    zone = signal.get("entry_zone") or {}
    low = _price(zone.get("low", signal.get("entry_price")))
    high = _price(zone.get("high", signal.get("entry_price")))
    if low > high:
        low, high = high, low
    return low, high


def _tp1(signal: dict[str, Any]) -> float:
    values = signal.get("take_profits") or []
    return _price(values[0]) if values else 0.0


def _valid_geometry(signal: dict[str, Any]) -> bool:
    direction = str(signal.get("direction") or "").upper()
    low, high = _entry_zone(signal)
    tp1 = _tp1(signal)
    sl = _price(signal.get("stop_loss"))
    if direction == "LONG":
        return low > 0 and high >= low and sl > 0 and sl < low and tp1 > high
    if direction == "SHORT":
        return low > 0 and high >= low and sl > high and tp1 > 0 and tp1 < low
    return False


def candle_event(signal: dict[str, Any], candle: dict[str, Any]) -> dict[str, Any] | None:
    """Classify a completed one-minute candle as the bot does."""
    if not _valid_geometry(signal):
        return {"status": STATUS_CANCELLED, "reason": CANCEL_INVALID_GEOMETRY}
    low_c = _price(candle.get("low"))
    high_c = _price(candle.get("high"))
    if low_c <= 0 or high_c <= 0:
        return None
    zone_low, zone_high = _entry_zone(signal)
    tp1 = _tp1(signal)
    sl = _price(signal.get("stop_loss"))
    direction = str(signal["direction"]).upper()
    touched = low_c <= zone_high and high_c >= zone_low
    tp_hit = high_c >= tp1 if direction == "LONG" else low_c <= tp1
    sl_hit = low_c <= sl if direction == "LONG" else high_c >= sl
    event_at = _utc_naive(candle.get("close_time") or candle.get("open_time"))
    if touched and sl_hit:
        return {"status": STATUS_CANCELLED, "reason": CANCEL_AMBIGUOUS_ENTRY_SL, "event_at": event_at}
    if touched and tp_hit:
        return {"status": STATUS_CANCELLED, "reason": CANCEL_AMBIGUOUS_INTRABAR, "event_at": event_at}
    if sl_hit:
        return {"status": STATUS_CANCELLED, "reason": CANCEL_PRE_ENTRY_INVALIDATED, "event_at": event_at, "event_price": sl}
    if tp_hit:
        return {"status": STATUS_CANCELLED, "reason": CANCEL_TP_REACHED_BEFORE_ENTRY, "event_at": event_at, "event_price": tp1}
    if touched:
        touch_price = zone_high if direction == "LONG" else zone_low
        return {"status": STATUS_TOUCHED, "reason": "ENTRY_ZONE_TOUCHED", "event_at": event_at, "event_price": touch_price}
    return None


def validate_prepublication_setup(
    signal: dict[str, Any], *, current_price: float, candles: Iterable[dict[str, Any]],
) -> tuple[bool, str | None]:
    """Mirror the bot's final check immediately before creating/publishing a signal."""
    if not _valid_geometry(signal):
        return False, CANCEL_INVALID_GEOMETRY
    direction = str(signal["direction"]).upper()
    zone_low, zone_high = _entry_zone(signal)
    price = _price(current_price)
    tp1 = _tp1(signal)
    if price <= 0:
        return False, "CURRENT_PRICE_UNAVAILABLE"
    if direction == "LONG":
        if price <= zone_high:
            return False, "ENTRY_ALREADY_TOUCHED"
        if price >= tp1:
            return False, CANCEL_TP_REACHED_BEFORE_ENTRY
        gap = price - zone_high
    else:
        if price >= zone_low:
            return False, "ENTRY_ALREADY_TOUCHED"
        if price <= tp1:
            return False, CANCEL_TP_REACHED_BEFORE_ENTRY
        gap = zone_low - price
    # HADES strategies already apply their own first-touch/reset and distance
    # rules. Keep KERYNTIX's legacy ATR-distance gate only for legacy candidates.
    strategy_name = str(signal.get("strategy_name") or "").strip().lower()
    if strategy_name not in {"breakout_reset", "liquidity_sweep_reversal"}:
        atr_value = _price(signal.get("atr_value"))
        if atr_value > 0:
            distance_atr = gap / atr_value
            if distance_atr < MIN_PREENTRY_DISTANCE_ATR:
                return False, "ENTRY_ZONE_TOO_CLOSE_TO_PUBLISH"
            if distance_atr > MAX_PREENTRY_DISTANCE_ATR:
                return False, "ENTRY_ZONE_TOO_FAR_TO_PUBLISH"
    for candle in candles:
        event = candle_event(signal, candle)
        if event:
            if event["status"] == STATUS_TOUCHED:
                return False, "ENTRY_ALREADY_TOUCHED"
            return False, str(event.get("reason") or "PREPUBLICATION_INVALIDATED")
    return True, None
