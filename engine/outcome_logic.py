"""Post-entry trade tracking (pure logic, no Mongo/Telegram/network).

A signal enters tracking when the lifecycle marks it TOUCHED. From then on it has
NO expiry: it stays open until it resolves.

Resolution rules (R is always measured against the signal's own geometry):
    * SL before TP1                -> result SL,  r_real = -1
    * TP1 then TP2                 -> result TP2, r_real = R(TP2)  (not R(TP1)+R(TP2))
    * TP1 then back to SL          -> result TP1, r_real = R(TP1)  (stays a TP1 win)
    * signal with a single TP, TP1 -> result TP1 and closed immediately

Fill model: the entry is assumed filled at the zone edge first reached by price
(LONG: zone high, SHORT: zone low). That is the same event that marks TOUCHED.

Candle model: only CLOSED 1m candles are evaluated, chronologically. When one
candle contains both the adverse and the favourable level, the order cannot be
known, so the conservative outcome is recorded and `ambiguous=True` is stored so
it stays auditable.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

STATE_OPEN = "OPEN"
STATE_TP1_HIT = "TP1_HIT"
STATE_CLOSED = "CLOSED"
OPEN_STATES = (STATE_OPEN, STATE_TP1_HIT)

RESULT_TP1 = "TP1"
RESULT_TP2 = "TP2"
RESULT_SL = "SL"
RESULT_INVALID = "INVALID"
WIN_RESULTS = (RESULT_TP1, RESULT_TP2)

EVENT_TP1_HIT = "TP1_HIT"
EVENT_CLOSED = "CLOSED"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def trade_levels(signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Fill, SL, TPs and real R multiples. None if the geometry is unusable."""
    direction = str(signal.get("direction") or "").upper()
    if direction not in ("LONG", "SHORT"):
        return None

    zone = signal.get("entry_zone") or {}
    entry = _f(signal.get("entry_price"))
    low = _f(zone.get("low"), entry)
    high = _f(zone.get("high"), entry)
    if low > high:
        low, high = high, low

    fill = high if direction == "LONG" else low
    if fill <= 0:
        fill = entry
    sl = _f(signal.get("stop_loss"))
    tps = [v for v in (_f(x) for x in (signal.get("take_profits") or [])) if v > 0][:2]
    if fill <= 0 or sl <= 0 or not tps:
        return None

    if direction == "LONG":
        valid = sl < fill < tps[0] and (len(tps) == 1 or tps[1] > tps[0])
    else:
        valid = sl > fill > tps[0] and (len(tps) == 1 or tps[1] < tps[0])
    if not valid:
        return None

    risk = abs(fill - sl)
    return {
        "direction": direction,
        "fill": fill,
        "sl": sl,
        "tp1": tps[0],
        "tp2": tps[1] if len(tps) > 1 else None,
        "risk": risk,
        "r_tp1": abs(tps[0] - fill) / risk,
        "r_tp2": (abs(tps[1] - fill) / risk) if len(tps) > 1 else None,
    }


def _hits(levels: Dict[str, Any], candle: Dict[str, Any]):
    high = _f(candle.get("high"))
    low = _f(candle.get("low"))
    if high <= 0 or low <= 0:
        return False, False, False
    if levels["direction"] == "LONG":
        sl_hit = low <= levels["sl"]
        tp1_hit = high >= levels["tp1"]
        tp2_hit = levels["tp2"] is not None and high >= levels["tp2"]
    else:
        sl_hit = high >= levels["sl"]
        tp1_hit = low <= levels["tp1"]
        tp2_hit = levels["tp2"] is not None and low <= levels["tp2"]
    return sl_hit, tp1_hit, tp2_hit


def advance_trade(
    signal: Dict[str, Any],
    candles: Iterable[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Apply new closed 1m candles to an open trade.

    Returns {"outcome": <updated outcome dict>, "events": [...], "closed": dict|None,
    "levels": dict|None, "advanced": bool}. Pure: does not mutate `signal`.
    """
    now = _dt(now) or datetime.utcnow()
    outcome: Dict[str, Any] = dict(signal.get("outcome") or {})
    state = outcome.get("state") or STATE_OPEN
    result: Dict[str, Any] = {
        "outcome": outcome, "events": [], "closed": None, "levels": None, "advanced": False,
    }
    if state not in OPEN_STATES:
        return result

    touched_at = _dt(signal.get("touched_at")) or _dt(outcome.get("tracked_until"))
    tracked_until = _dt(outcome.get("tracked_until")) or touched_at
    tp1_at = _dt(outcome.get("tp1_at"))

    levels = trade_levels(signal)
    result["levels"] = levels
    if levels is None:
        outcome.update({"state": STATE_CLOSED, "result": RESULT_INVALID, "closed_at": now})
        result["advanced"] = True
        return result

    def close(res: str, at: datetime, *, ambiguous: bool, via: str) -> None:
        r_map = {RESULT_TP1: levels["r_tp1"], RESULT_TP2: levels["r_tp2"], RESULT_SL: -1.0}
        r_real = round(float(r_map[res]), 4)
        outcome.update({
            "state": STATE_CLOSED, "result": res, "closed_at": at,
            "tp1_at": tp1_at, "tracked_until": at,
        })
        result["closed"] = {
            "result": res,
            "outcome": "win" if res in WIN_RESULTS else "loss",
            "r_real": r_real,
            "ambiguous": ambiguous,
            "closed_at": at,
            "tp1_at": tp1_at,
        }
        result["events"].append({
            "type": EVENT_CLOSED, "result": res, "via": via, "at": at,
            "r": r_real, "r_tp1": round(levels["r_tp1"], 4), "ambiguous": ambiguous,
        })

    ordered = sorted(candles, key=lambda c: _dt(c.get("open_time")) or datetime.min)
    for candle in ordered:
        open_at = _dt(candle.get("open_time"))
        close_at = _dt(candle.get("close_time"))
        if open_at is None or close_at is None:
            continue
        if touched_at is not None and open_at < touched_at:
            continue
        if tracked_until is not None and close_at <= tracked_until:
            continue
        if close_at > now:
            break  # still-forming candle: never evaluated, keeps results deterministic

        tracked_until = close_at
        result["advanced"] = True
        outcome["tracked_until"] = close_at
        sl_hit, tp1_hit, tp2_hit = _hits(levels, candle)

        current = outcome.get("state") or STATE_OPEN

        if current == STATE_OPEN:
            if sl_hit:
                # SL and TP1 inside one candle: order unknown -> conservative SL.
                close(RESULT_SL, close_at, ambiguous=tp1_hit, via="SL")
                break
            if tp2_hit:
                tp1_at = tp1_at or close_at
                close(RESULT_TP2, close_at, ambiguous=False, via="TP2")
                break
            if tp1_hit:
                tp1_at = close_at
                if levels["tp2"] is None:
                    close(RESULT_TP1, close_at, ambiguous=False, via="TP1")
                    break
                outcome.update({"state": STATE_TP1_HIT, "tp1_at": tp1_at})
                result["events"].append({
                    "type": EVENT_TP1_HIT, "result": RESULT_TP1, "at": close_at,
                    "r": round(levels["r_tp1"], 4), "r_tp1": round(levels["r_tp1"], 4),
                    "ambiguous": False,
                })
        else:  # TP1_HIT: SL is still the original one by rule
            if sl_hit and tp2_hit:
                close(RESULT_TP1, close_at, ambiguous=True, via="SL_AFTER_TP1")
                break
            if tp2_hit:
                close(RESULT_TP2, close_at, ambiguous=False, via="TP2")
                break
            if sl_hit:
                close(RESULT_TP1, close_at, ambiguous=False, via="SL_AFTER_TP1")
                break

    return result


def build_result_doc(signal: Dict[str, Any], levels: Dict[str, Any], closed: Dict[str, Any]) -> Dict[str, Any]:
    """Immutable snapshot stored in signal_results (survives signal cleanup)."""
    touched_at = _dt(signal.get("touched_at"))
    closed_at = closed["closed_at"]
    zone = signal.get("entry_zone") or {}
    signal_id = str(signal.get("id") or signal.get("signal_id") or signal.get("_id"))
    return {
        "id": signal_id,
        "schema_version": str(signal.get("schema_version") or "1.0"),
        "signal_id": signal_id,
        "symbol": signal.get("symbol"),
        "direction": levels["direction"],
        "visibility": signal.get("visibility"),
        "entry_fill": levels["fill"],
        "entry_zone": {"low": zone.get("low"), "high": zone.get("high")},
        "stop_loss": levels["sl"],
        "take_profits": [v for v in (levels["tp1"], levels["tp2"]) if v is not None],
        "risk": levels["risk"],
        "r_tp1": round(levels["r_tp1"], 4),
        "r_tp2": round(levels["r_tp2"], 4) if levels["r_tp2"] is not None else None,
        "result": closed["result"],
        "outcome": closed["outcome"],
        "r_real": closed["r_real"],
        "ambiguous": bool(closed["ambiguous"]),
        "score": signal.get("score"),
        "created_at": _dt(signal.get("created_at")),
        "touched_at": touched_at,
        "tp1_at": closed.get("tp1_at"),
        "closed_at": closed_at,
        "evaluated_at": closed_at,
        "duration_minutes": (
            round((closed_at - touched_at).total_seconds() / 60.0, 1) if touched_at else None
        ),
    }
