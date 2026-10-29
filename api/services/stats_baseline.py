"""Administrative statistics baseline.

Resetting statistics never deletes closed trades. It stores a UTC cut-off date;
every public statistics endpoint then counts only trades closed on or after it,
so a modified strategy can be measured from zero while the full history stays
in the database and can be restored.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from api.repositories.signals import RepositoryUnavailable, _parse_datetime

STATS_BASELINE_KEY = "stats_baseline"


def _iso(value: Any) -> str | None:
    parsed = _parse_datetime(value)
    return parsed.isoformat().replace("+00:00", "Z") if parsed else None


def get_reset_at(business_repository: Any) -> datetime | None:
    """Return the active statistics cut-off (UTC) or None when never reset."""
    getter = getattr(business_repository, "get_setting", None)
    if getter is None:
        return None
    try:
        doc = getter(STATS_BASELINE_KEY)
    except Exception as exc:  # storage failure must not silently show old numbers
        raise RepositoryUnavailable("No se pudo leer el punto de reinicio de estadísticas") from exc
    if not doc:
        return None
    return _parse_datetime(doc.get("reset_at"))


def rows_since(rows: Sequence[Mapping[str, Any]], reset_at: datetime | None) -> list[Mapping[str, Any]]:
    if reset_at is None:
        return list(rows)
    kept = []
    for row in rows:
        closed = _parse_datetime(row.get("closed_at"))
        if closed is not None and closed >= reset_at:
            kept.append(row)
    return kept


def latest_start(start: datetime | None, reset_at: datetime | None) -> datetime | None:
    """Lower bound combining the requested period and the reset cut-off."""
    if reset_at is None:
        return start
    if start is None:
        return reset_at
    start_utc = _parse_datetime(start) or reset_at
    return max(start_utc, reset_at)


def describe(business_repository: Any, signal_repository: Any) -> dict[str, Any]:
    doc = (business_repository.get_setting(STATS_BASELINE_KEY) if hasattr(business_repository, "get_setting") else None) or {}
    reset_at = _parse_datetime(doc.get("reset_at"))
    all_rows = signal_repository.list_results_for_period(start=None)
    counted = len(rows_since(all_rows, reset_at))
    return {
        "active": reset_at is not None,
        "reset_at": _iso(reset_at),
        "reset_by": doc.get("reset_by"),
        "reason": doc.get("reason"),
        "trades_counted": counted,
        "trades_archived": len(all_rows) - counted,
        "trades_total": len(all_rows),
    }


def apply_reset(business_repository: Any, *, actor_id: str, reason: str, now: datetime | None = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    business_repository.set_setting(STATS_BASELINE_KEY, {
        "reset_at": moment, "reset_by": str(actor_id), "reason": reason,
    })
    return moment


def clear_reset(business_repository: Any) -> None:
    business_repository.set_setting(STATS_BASELINE_KEY, None)
