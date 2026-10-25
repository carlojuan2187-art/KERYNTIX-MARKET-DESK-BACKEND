"""Pure signal lifecycle rules ported from the original FTrade Telegram bot.

No database, Telegram, HTTP client or scheduler is imported here. The lifecycle
worker supplies prices/candles and persists the returned transitions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Tuple

STATUS_PENDING = "PENDING"
STATUS_TOUCHED = "TOUCHED"
STATUS_CANCELLED = "CANCELLED"

CANCEL_ENTRY_NOT_TOUCHED = "ENTRY_NOT_TOUCHED"
CANCEL_TP_REACHED_BEFORE_ENTRY = "TP_REACHED_BEFORE_ENTRY"
CANCEL_INVALID_GEOMETRY = "INVALID_GEOMETRY"
CANCEL_AMBIGUOUS_INTRABAR = "AMBIGUOUS_ENTRY_TP_ORDER"
CANCEL_PRE_ENTRY_INVALIDATED = "PRE_ENTRY_INVALIDATED"
CANCEL_AMBIGUOUS_ENTRY_SL = "AMBIGUOUS_ENTRY_SL_ORDER"
CANCEL_STALE_TOUCH = "STALE_ENTRY_TOUCH"

TOUCHED_DISPLAY_MINUTES = max(5, int(__import__("os").getenv("TOUCHED_DISPLAY_MINUTES", "30")))
TOUCH_ALERT_MAX_AGE_SECONDS = max(15, int(__import__("os").getenv("TOUCH_ALERT_MAX_AGE_SECONDS", "75")))
TOUCH_MAX_DISTANCE_ATR = max(0.05, float(__import__("os").getenv("TOUCH_MAX_DISTANCE_ATR", "0.25")))
CANCELLED_DISPLAY_MINUTES = max(5, int(__import__("os").getenv("CANCELLED_DISPLAY_MINUTES", "30")))
MIN_PREENTRY_DISTANCE_ATR = float(__import__("os").getenv("LSR_MIN_PREENTRY_DISTANCE_ATR", "0.10"))
MAX_PREENTRY_DISTANCE_ATR = float(__import__("os").getenv("LSR_MAX_PREENTRY_DISTANCE_ATR", "0.85"))


def utc_naive(value: Any) -> Optional[datetime]:
    """Normalize a datetime to naive UTC, matching legacy bot storage semantics."""
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _price(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _entry_zone(signal: dict[str, Any]) -> Tuple[float, float]:
    zone = signal.get("entry_zone") or {}
    low = _price(zone.get("low", signal.get("entry_price")))
    high = _price(zone.get("high", signal.get("entry_price")))
    if low > high:
        low, high = high, low
    return low, high


def _tp1(signal: dict[str, Any]) -> float:
    values = signal.get("take_profits") or []
    return _price(values[0]) if values else 0.0


def valid_geometry(signal: dict[str, Any]) -> bool:
    direction = str(signal.get("direction") or "").upper()
    low, high = _entry_zone(signal)
    tp1 = _tp1(signal)
    sl = _price(signal.get("stop_loss"))
    if direction == "LONG":
        return low > 0 and high >= low and sl > 0 and sl < low and tp1 > high
    if direction == "SHORT":
        return low > 0 and high >= low and sl > high and tp1 > 0 and tp1 < low
    return False


def current_price_event(signal: dict[str, Any], current_price: float) -> Optional[dict[str, Any]]:
    """Evaluate a live ticker without manufacturing historical fills."""
    if not valid_geometry(signal):
        return {"status": STATUS_CANCELLED, "reason": CANCEL_INVALID_GEOMETRY}
    price = _price(current_price)
    if price <= 0:
        return None
    direction = str(signal["direction"]).upper()
    low, high = _entry_zone(signal)
    tp1 = _tp1(signal)
    sl = _price(signal.get("stop_loss"))
    if direction == "LONG":
        if price <= sl:
            return {"status": STATUS_CANCELLED, "reason": CANCEL_PRE_ENTRY_INVALIDATED, "event_price": price}
        if price >= tp1:
            return {"status": STATUS_CANCELLED, "reason": CANCEL_TP_REACHED_BEFORE_ENTRY, "event_price": price}
    else:
        if price >= sl:
            return {"status": STATUS_CANCELLED, "reason": CANCEL_PRE_ENTRY_INVALIDATED, "event_price": price}
        if price <= tp1:
            return {"status": STATUS_CANCELLED, "reason": CANCEL_TP_REACHED_BEFORE_ENTRY, "event_price": price}
    if low <= price <= high:
        return {"status": STATUS_TOUCHED, "reason": "ENTRY_ZONE_TOUCHED", "event_price": price}
    return None


def candle_event(signal: dict[str, Any], candle: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Classify one completed 1m candle for a pending setup."""
    if not valid_geometry(signal):
        return {"status": STATUS_CANCELLED, "reason": CANCEL_INVALID_GEOMETRY}
    candle_low = _price(candle.get("low"))
    candle_high = _price(candle.get("high"))
    if candle_low <= 0 or candle_high <= 0:
        return None
    zone_low, zone_high = _entry_zone(signal)
    tp1 = _tp1(signal)
    sl = _price(signal.get("stop_loss"))
    direction = str(signal["direction"]).upper()
    touched = candle_low <= zone_high and candle_high >= zone_low
    tp_hit = candle_high >= tp1 if direction == "LONG" else candle_low <= tp1
    sl_hit = candle_low <= sl if direction == "LONG" else candle_high >= sl
    event_at = utc_naive(candle.get("close_time") or candle.get("open_time"))
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


def _touch_is_actionable(signal: dict[str, Any], touch_event: dict[str, Any], now: datetime, current_price: Optional[float]) -> bool:
    event_at = utc_naive(touch_event.get("event_at")) or now
    if (now - event_at).total_seconds() > TOUCH_ALERT_MAX_AGE_SECONDS:
        return False
    price = _price(current_price)
    if price <= 0:
        return True
    zone_low, zone_high = _entry_zone(signal)
    atr = max(_price(signal.get("atr_value")), abs(_price(signal.get("entry_price"))) * 0.0003, 1e-9)
    if zone_low <= price <= zone_high:
        return True
    distance = (zone_low - price) if price < zone_low else (price - zone_high)
    return distance <= atr * TOUCH_MAX_DISTANCE_ATR


def evaluate_pending_signal(
    signal: dict[str, Any],
    candles: Iterable[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    current_price: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Return a safe PENDING -> TOUCHED/CANCELLED transition or None."""
    now = utc_naive(now) or datetime.utcnow()
    created_at = utc_naive(signal.get("created_at")) or now
    expires_at = utc_naive(signal.get("entry_expires_at") or signal.get("telegram_valid_until"))
    if current_price is not None:
        live_event = current_price_event(signal, current_price)
        if live_event and live_event.get("status") == STATUS_CANCELLED:
            live_event.setdefault("event_at", now)
            return live_event
    touch_candidate: Optional[dict[str, Any]] = None
    ordered = sorted(list(candles), key=lambda c: utc_naive(c.get("open_time") or c.get("close_time")) or datetime.min)
    for candle in ordered:
        open_at = utc_naive(candle.get("open_time"))
        if open_at is not None and open_at < created_at:
            continue
        event = candle_event(signal, candle)
        if not event:
            continue
        event.setdefault("event_at", utc_naive(candle.get("close_time")) or now)
        if event["status"] == STATUS_CANCELLED:
            return event
        if touch_candidate is None:
            touch_candidate = event
    if touch_candidate is not None:
        if _touch_is_actionable(signal, touch_candidate, now, current_price):
            return touch_candidate
        return {
            "status": STATUS_CANCELLED,
            "reason": CANCEL_STALE_TOUCH,
            "event_at": now,
            "event_price": _price(current_price) or touch_candidate.get("event_price"),
        }
    if current_price is not None:
        live_event = current_price_event(signal, current_price)
        if live_event:
            live_event.setdefault("event_at", now)
            return live_event
    if expires_at is not None and now >= expires_at:
        return {"status": STATUS_CANCELLED, "reason": CANCEL_ENTRY_NOT_TOUCHED, "event_at": now}
    return None


def display_until_for(status: str, event_at: datetime) -> datetime:
    if status == STATUS_TOUCHED:
        return event_at + timedelta(minutes=TOUCHED_DISPLAY_MINUTES)
    return event_at + timedelta(minutes=CANCELLED_DISPLAY_MINUTES)


def transition_update(signal: dict[str, Any], event: dict[str, Any], *, recipient_ids: list[int] | None = None) -> dict[str, Any] | None:
    """Build root and canonical nested fields for an atomic Mongo transition."""
    new_status = str(event.get("status") or "").upper()
    if new_status not in {STATUS_TOUCHED, STATUS_CANCELLED}:
        return None
    event_at = utc_naive(event.get("event_at")) or datetime.utcnow()
    reason = str(event.get("reason") or "")
    fields: dict[str, Any] = {
        "status": new_status,
        "status_updated_at": event_at,
        "display_until": display_until_for(new_status, event_at),
        "lifecycle.status": new_status,
        "lifecycle.status_updated_at": event_at,
        "lifecycle.display_until": display_until_for(new_status, event_at),
    }
    if new_status == STATUS_TOUCHED:
        fields.update({
            "touched_at": event_at,
            "touch_price": _price(event.get("event_price")) or None,
            "cancelled_at": None,
            "cancel_reason": None,
            "lifecycle.touched_at": event_at,
            "lifecycle.touch_price": _price(event.get("event_price")) or None,
            "lifecycle.cancelled_at": None,
            "lifecycle.cancel_reason": None,
            "outcome": {
                "state": "OPEN", "result": None, "tracked_until": event_at,
                "tp1_at": None, "closed_at": None,
                # Web notifications are resolved through user access at delivery time.
                "recipient_ids": list(recipient_ids or []),
            },
        })
    else:
        fields.update({
            "cancelled_at": event_at,
            "cancel_reason": reason,
            "lifecycle.cancelled_at": event_at,
            "lifecycle.cancel_reason": reason,
        })
    return fields
