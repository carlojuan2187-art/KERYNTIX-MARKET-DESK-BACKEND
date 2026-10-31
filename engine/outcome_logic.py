"""Post-entry trade tracking (pure logic, no Mongo/Telegram/network).

A signal is modelled as one full position entered at the strategy's canonical
``entry_price``. The entry zone is a publication/touch window, not a different
fill price. Risk and target R multiples therefore use the same reference that
was used by the strategy to build SL/TP1/TP2.

Resolution rules:
    * SL before TP1                         -> -1R
    * TP1, then TP2                         -> R(TP2)
    * TP1, then retrace to protected TP1    -> R(TP1)
    * single-target signal reaches TP1      -> R(TP1), closed

This is a single-full-position simulation; it does not model partial exits,
exchange fees, funding, spread or slippage. TP1 activates a protective stop at
TP1 for subsequent market observations. Closed 1m candles remain the fallback;
fresh market-feed snapshots can advance a level sooner when available.

Candle ambiguity is conservative: if a candle can contain both TP2 and a return
to the TP1 protection level, the model records TP1 and marks it ambiguous.
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
CALCULATION_VERSION = "2.0"
EXECUTION_MODEL = "SIMULATED_FULL_POSITION_TP1_PROTECTION"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _dt(value: Any) -> Optional[datetime]:
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
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def trade_levels(signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return trade geometry using the same modeled entry as the strategy.

    ``entry_zone`` only determines when a pending signal becomes actionable.
    It must not silently replace ``entry_price`` when calculating R multiples.
    """
    direction = str(signal.get("direction") or "").upper()
    if direction not in ("LONG", "SHORT"):
        return None

    entry = _f(signal.get("entry_price"))
    zone = signal.get("entry_zone") or {}
    low = _f(zone.get("low"), entry)
    high = _f(zone.get("high"), entry)
    if low > high:
        low, high = high, low
    sl = _f(signal.get("stop_loss"))
    tps = [v for v in (_f(x) for x in (signal.get("take_profits") or [])) if v > 0][:2]
    if entry <= 0 or sl <= 0 or not tps:
        return None
    # For strategy-produced signals the canonical entry must be inside its zone.
    # Older records with missing zone bounds are treated as a point zone.
    if low <= 0 or high <= 0 or not (low <= entry <= high):
        return None

    if direction == "LONG":
        valid = sl < entry < tps[0] and (len(tps) == 1 or tps[1] > tps[0])
    else:
        valid = sl > entry > tps[0] and (len(tps) == 1 or tps[1] < tps[0])
    if not valid:
        return None

    risk = abs(entry - sl)
    return {
        "direction": direction,
        "fill": entry,
        "entry_reference": "entry_price",
        "sl": sl,
        "tp1": tps[0],
        "tp2": tps[1] if len(tps) > 1 else None,
        "risk": risk,
        "r_tp1": abs(tps[0] - entry) / risk,
        "r_tp2": (abs(tps[1] - entry) / risk) if len(tps) > 1 else None,
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


def _protective_tp1_hit(levels: Dict[str, Any], candle: Dict[str, Any]) -> bool:
    high = _f(candle.get("high"))
    low = _f(candle.get("low"))
    if high <= 0 or low <= 0:
        return False
    return low <= levels["tp1"] if levels["direction"] == "LONG" else high >= levels["tp1"]


def advance_trade(
    signal: Dict[str, Any],
    candles: Iterable[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    live_price: Optional[float] = None,
    live_price_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Apply closed 1m candles and, optionally, one fresh market-feed observation.

    Live observations are snapshots, not exchange executions. They allow the
    simulated state to advance on the next worker pass (normally 2 seconds),
    while closed 1m candles remain a deterministic recovery/fallback source.
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
    tp2_at = _dt(outcome.get("tp2_at"))
    protection_armed_at = _dt(outcome.get("protection_armed_at"))

    levels = trade_levels(signal)
    result["levels"] = levels
    if levels is None:
        outcome.update({"state": STATE_CLOSED, "result": RESULT_INVALID, "closed_at": now})
        result["advanced"] = True
        return result

    def close(res: str, at: datetime, *, ambiguous: bool, via: str, exit_price: float) -> None:
        nonlocal tp1_at, tp2_at
        r_map = {RESULT_TP1: levels["r_tp1"], RESULT_TP2: levels["r_tp2"], RESULT_SL: -1.0}
        r_real = round(float(r_map[res]), 4)
        if res in WIN_RESULTS and tp1_at is None:
            tp1_at = at
        if res == RESULT_TP2 and tp2_at is None:
            tp2_at = at
        outcome.update({
            "state": STATE_CLOSED,
            "result": res,
            "closed_at": at,
            "tp1_at": tp1_at,
            "tp2_at": tp2_at,
            "tracked_until": at,
            "protective_stop_price": levels["tp1"] if tp1_at is not None else None,
            "protection_armed_at": protection_armed_at,
            "exit_price": float(exit_price),
            "exit_via": via,
            "calculation_version": CALCULATION_VERSION,
            "execution_model": EXECUTION_MODEL,
        })
        result["closed"] = {
            "result": res,
            "outcome": "win" if res in WIN_RESULTS else "loss",
            "r_real": r_real,
            "ambiguous": ambiguous,
            "closed_at": at,
            "tp1_at": tp1_at,
            "tp2_at": tp2_at,
            "exit_price": float(exit_price),
            "via": via,
            "calculation_version": CALCULATION_VERSION,
            "execution_model": EXECUTION_MODEL,
        }
        result["events"].append({
            "type": EVENT_CLOSED,
            "result": res,
            "via": via,
            "at": at,
            "r": r_real,
            "r_tp1": round(levels["r_tp1"], 4),
            "exit_price": float(exit_price),
            "ambiguous": ambiguous,
        })
        result["advanced"] = True

    def mark_tp1(at: datetime, *, via: str) -> None:
        nonlocal tp1_at, protection_armed_at
        if tp1_at is None:
            tp1_at = at
        protection_armed_at = at
        outcome.update({
            "state": STATE_TP1_HIT,
            "tp1_at": tp1_at,
            "protective_stop_price": levels["tp1"],
            "protection_armed_at": protection_armed_at,
        })
        result["events"].append({
            "type": EVENT_TP1_HIT,
            "result": RESULT_TP1,
            "at": at,
            "r": round(levels["r_tp1"], 4),
            "r_tp1": round(levels["r_tp1"], 4),
            "protective_stop_price": levels["tp1"],
            "via": via,
            "ambiguous": False,
        })
        result["advanced"] = True

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
        outcome["tracked_until"] = close_at
        result["advanced"] = True
        sl_hit, tp1_hit, tp2_hit = _hits(levels, candle)
        current = outcome.get("state") or STATE_OPEN

        if current == STATE_OPEN:
            if sl_hit:
                # SL and TP1 in one candle: order unknown; keep the conservative SL.
                close(RESULT_SL, close_at, ambiguous=tp1_hit, via="SL", exit_price=levels["sl"])
                break
            if tp2_hit:
                # TP2 implies TP1 was crossed first in directional price space. If
                # the same candle also returns through TP1, order is unknowable; use
                # the protected TP1 outcome rather than overstate the TP2 result.
                mark_tp1(close_at, via="TP2_SAME_CANDLE")
                if _protective_tp1_hit(levels, candle):
                    close(
                        RESULT_TP1, close_at, ambiguous=True,
                        via="TP1_TP2_SAME_CANDLE_PROTECTIVE_AMBIGUOUS",
                        exit_price=levels["tp1"],
                    )
                else:
                    tp2_at = close_at
                    close(RESULT_TP2, close_at, ambiguous=False, via="TP2", exit_price=levels["tp2"])
                break
            if tp1_hit:
                mark_tp1(close_at, via="TP1_CANDLE")
                if levels["tp2"] is None:
                    close(RESULT_TP1, close_at, ambiguous=False, via="TP1_ONLY", exit_price=levels["tp1"])
                    break
        else:  # TP1_HIT: protective stop at TP1 is active, original SL is retired.
            # If TP1 protection was armed part-way through this same candle, its
            # earlier low/high cannot prove a post-activation stop-out. Keep it
            # open unless TP2 and the protective area both appear (ambiguous);
            # fresh feed observations can resolve the sequence sooner.
            activation_inside_candle = (
                protection_armed_at is not None and open_at < protection_armed_at <= close_at
            )
            protective_hit = _protective_tp1_hit(levels, candle)
            if activation_inside_candle:
                if sl_hit:
                    # The candle includes the live TP1 activation and the original
                    # stop level. OHLC cannot prove which came first, so resolve
                    # conservatively as the original -1R stop.
                    close(
                        RESULT_SL, close_at, ambiguous=True,
                        via="SL_TP1_ACTIVATION_CANDLE_AMBIGUOUS",
                        exit_price=levels["sl"],
                    )
                    break
                if tp2_hit and protective_hit:
                    close(
                        RESULT_TP1, close_at, ambiguous=True,
                        via="TP1_PROTECTION_AMBIGUOUS_ACTIVATION_CANDLE",
                        exit_price=levels["tp1"],
                    )
                    break
                if tp2_hit:
                    tp2_at = close_at
                    close(RESULT_TP2, close_at, ambiguous=False, via="TP2", exit_price=levels["tp2"])
                    break
                # A low/high on this activation candle may predate the exact live
                # TP1 tick. Wait for a subsequent observation to test the new stop.
                continue
            if protective_hit and tp2_hit:
                # With TP1 protection already active, unknown intrabar order is
                # resolved conservatively at the protected TP1 level.
                close(
                    RESULT_TP1, close_at, ambiguous=True,
                    via="TP1_PROTECTIVE_STOP_AMBIGUOUS",
                    exit_price=levels["tp1"],
                )
                break
            if protective_hit:
                close(
                    RESULT_TP1, close_at, ambiguous=False,
                    via="TP1_PROTECTIVE_STOP",
                    exit_price=levels["tp1"],
                )
                break
            if tp2_hit:
                tp2_at = close_at
                close(RESULT_TP2, close_at, ambiguous=False, via="TP2", exit_price=levels["tp2"])
                break

    # Live feed snapshot: process only a new observation that is newer than the
    # latest closed candle already consumed. Persistence is event-driven; we do
    # not write every unchanged ticker update to MongoDB.
    live_at = _dt(live_price_at)
    price = _f(live_price)
    last_live_at = _dt(outcome.get("last_live_price_at"))
    if (
        result["closed"] is None
        and live_at is not None
        and price > 0
        and (last_live_at is None or live_at > last_live_at)
        and (tracked_until is None or live_at > tracked_until)
    ):
        current = outcome.get("state") or STATE_OPEN
        direction = levels["direction"]
        adverse_hit = price <= levels["sl"] if direction == "LONG" else price >= levels["sl"]
        tp1_hit = price >= levels["tp1"] if direction == "LONG" else price <= levels["tp1"]
        tp2_hit = levels["tp2"] is not None and (
            price >= levels["tp2"] if direction == "LONG" else price <= levels["tp2"]
        )
        protective_hit = price <= levels["tp1"] if direction == "LONG" else price >= levels["tp1"]

        if current == STATE_OPEN:
            if adverse_hit:
                close(RESULT_SL, live_at, ambiguous=False, via="SL_LIVE_PRICE", exit_price=levels["sl"])
            elif tp2_hit:
                mark_tp1(live_at, via="TP2_LIVE_PRICE")
                tp2_at = live_at
                close(RESULT_TP2, live_at, ambiguous=False, via="TP2_LIVE_PRICE", exit_price=levels["tp2"])
            elif tp1_hit:
                mark_tp1(live_at, via="TP1_LIVE_PRICE")
                if levels["tp2"] is None:
                    close(RESULT_TP1, live_at, ambiguous=False, via="TP1_ONLY_LIVE_PRICE", exit_price=levels["tp1"])
            if result["advanced"]:
                outcome["last_live_price_at"] = live_at
        else:  # TP1 protection armed on a prior observation/candle.
            if protective_hit:
                close(
                    RESULT_TP1, live_at, ambiguous=False,
                    via="TP1_PROTECTIVE_STOP_LIVE_PRICE",
                    exit_price=levels["tp1"],
                )
            elif tp2_hit:
                tp2_at = live_at
                close(RESULT_TP2, live_at, ambiguous=False, via="TP2_LIVE_PRICE", exit_price=levels["tp2"])
            if result["advanced"]:
                outcome["last_live_price_at"] = live_at

    return result


def build_result_doc(signal: Dict[str, Any], levels: Dict[str, Any], closed: Dict[str, Any]) -> Dict[str, Any]:
    """Immutable v2 snapshot stored in signal_results (survives signal cleanup)."""
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
        "entry_reference": levels.get("entry_reference", "entry_price"),
        "touch_price": signal.get("touch_price"),
        "entry_zone": {"low": zone.get("low"), "high": zone.get("high")},
        "stop_loss": levels["sl"],
        "take_profits": [v for v in (levels["tp1"], levels["tp2"]) if v is not None],
        "risk": levels["risk"],
        "r_tp1": round(levels["r_tp1"], 4),
        "r_tp2": round(levels["r_tp2"], 4) if levels["r_tp2"] is not None else None,
        "result": closed["result"],
        "outcome": closed["outcome"],
        "r_real": closed["r_real"],
        "exit_price": closed.get("exit_price"),
        "exit_via": closed.get("via"),
        "calculation_version": closed.get("calculation_version", CALCULATION_VERSION),
        "execution_model": closed.get("execution_model", EXECUTION_MODEL),
        "ambiguous": bool(closed["ambiguous"]),
        "score": signal.get("score"),
        "created_at": _dt(signal.get("created_at")),
        "touched_at": touched_at,
        "tp1_at": closed.get("tp1_at"),
        "tp2_at": closed.get("tp2_at"),
        "closed_at": closed_at,
        "evaluated_at": closed_at,
        "duration_minutes": (
            round((closed_at - touched_at).total_seconds() / 60.0, 1) if touched_at else None
        ),
    }
