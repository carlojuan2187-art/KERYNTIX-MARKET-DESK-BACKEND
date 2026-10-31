"""Closed-trade performance aggregation based on the original bot's statistics rules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Mapping, Sequence

UTC = timezone.utc
VALID_RESULTS = {"TP1", "TP2", "SL"}


def as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        if hasattr(value, "to_pydatetime"):
            try:
                value = value.to_pydatetime()
            except Exception:
                return None
        else:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def period_start(period: str, *, now: datetime | None = None) -> datetime | None:
    current = as_utc(now or datetime.now(UTC)) or datetime.now(UTC)
    if period == "all":
        return None
    day = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return day
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    raise ValueError("period debe ser day, week, month o all")


def _exit_price(row: Mapping[str, Any]) -> float | None:
    # New immutable v2 results store the exit level explicitly. Legacy snapshots
    # are reconstructed from their SL/TP fields as before.
    raw_exit = row.get("exit_price")
    try:
        explicit = float(raw_exit)
        if isfinite(explicit) and explicit > 0:
            return explicit
    except (TypeError, ValueError, OverflowError):
        pass
    result = str(row.get("result") or "").upper()
    if result == "SL":
        raw = row.get("stop_loss")
        if raw is None and isinstance(row.get("signal_snapshot"), Mapping):
            raw = row["signal_snapshot"].get("stop_loss")
    else:
        targets = row.get("take_profits")
        if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
            targets = []
        idx = 0 if result == "TP1" else 1
        raw = targets[idx] if len(targets) > idx else None
        if raw is None and isinstance(row.get("signal_snapshot"), Mapping):
            snapshot_targets = row["signal_snapshot"].get("take_profits") or []
            if isinstance(snapshot_targets, Sequence) and not isinstance(snapshot_targets, (str, bytes)) and len(snapshot_targets) > idx:
                raw = snapshot_targets[idx]
    try:
        price = float(raw)
        return price if price > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def build_performance(rows: Sequence[Mapping[str, Any]], *, period: str, now: datetime | None = None) -> dict[str, Any]:
    """Build the response consumed by Results page, using stored `r_real` values.

    Only closed TP1/TP2/SL records count. It never reconstructs an R value from
    prices and never fabricates an exit price when the immutable result lacks it.
    Periods follow the bot: UTC day, Monday-based UTC week, UTC calendar month, all.
    """
    start = period_start(period, now=now)
    closed: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        result = str(row.get("result") or "").upper()
        if result not in VALID_RESULTS:
            continue
        closed_at = as_utc(row.get("closed_at"))
        if closed_at is None or (start is not None and closed_at < start):
            continue
        try:
            r_real = float(row.get("r_real"))
            entry = float(row.get("entry_fill"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not (isfinite(entry) and entry > 0 and isfinite(r_real)):
            continue
        exit_price = _exit_price(row)
        if exit_price is None:
            # Do not drop the result from top-line stats, but do not invent a row
            # for the trade detail table if a legacy document lacks its exit level.
            exit_price = None
        closed.append({
            "id": str(row.get("id") or row.get("signal_id") or ""),
            "symbol": str(row.get("symbol") or "").upper(),
            "direction": str(row.get("direction") or "").upper(),
            "entry_price": entry,
            "exit_price": exit_price,
            "result": result,
            "r_result": r_real,
            "calculation_version": str(row.get("calculation_version") or "legacy_v1"),
            "execution_model": row.get("execution_model"),
            "entry_reference": row.get("entry_reference"),
            "exit_via": row.get("exit_via"),
            "ambiguous": bool(row.get("ambiguous", False)),
            "touched_at": as_utc(row.get("touched_at")),
            "closed_at": closed_at,
        })
    closed.sort(key=lambda item: item["closed_at"])

    wins = sum(1 for trade in closed if trade["result"] in {"TP1", "TP2"})
    operations = len(closed)
    r_values = [trade["r_result"] for trade in closed]
    total_r = round(sum(r_values), 2)
    best_r = max(r_values, default=0.0)
    worst_r = min(r_values, default=0.0)

    streak = 0
    if closed:
        last_won = closed[-1]["result"] in {"TP1", "TP2"}
        for trade in reversed(closed):
            won = trade["result"] in {"TP1", "TP2"}
            if won != last_won:
                break
            streak += 1 if won else -1

    accumulated = 0.0
    curve = [{"date": "Inicio", "r": 0.0}]
    for trade in closed:
        accumulated += trade["r_result"]
        curve.append({"date": trade["closed_at"].date().isoformat(), "r": round(accumulated, 4)})

    def iso(value: datetime | None) -> str:
        return value.isoformat().replace("+00:00", "Z") if value else ""

    public_trades = []
    for trade in reversed(closed):
        # `exit_price` is nullable in legacy rows; the UI contract expects a number.
        # We keep a `data_integrity_warnings` array so the client can avoid rendering
        # missing details as a price of zero.
        public_trades.append({
            **trade,
            "exit_price": trade["exit_price"],
            "touched_at": iso(trade["touched_at"]),
            "closed_at": iso(trade["closed_at"]),
        })

    return {
        "period": period,
        "operations": operations,
        "wins": wins,
        "losses": operations - wins,
        "win_rate": round(wins / operations * 100.0, 2) if operations else 0.0,
        "total_r": total_r,
        "average_r": round(total_r / operations, 2) if operations else 0.0,
        "best_r": round(best_r, 4),
        "worst_r": round(worst_r, 4),
        "streak": streak,
        "curve": curve,
        "trades": public_trades,
        "data_integrity_warnings": sum(1 for trade in closed if trade["exit_price"] is None),
        "legacy_results": sum(1 for trade in closed if trade["calculation_version"] != "2.0"),
        "calculation_version": "2.0",
        "period_start": start.isoformat().replace("+00:00", "Z") if start else None,
        "calculation_timezone": "UTC",
    }
