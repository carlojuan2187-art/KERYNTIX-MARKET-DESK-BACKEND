"""Personal performance projection from signals actually delivered to each account."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from api.repositories.signals import RepositoryUnavailable, _parse_datetime


def entitlement_intervals(claims: Mapping[str, Any]) -> list[tuple[datetime, datetime]]:
    """Return persisted trial/Premium windows; gaps remain gaps after renewal."""
    intervals: list[tuple[datetime, datetime]] = []
    created = _parse_datetime(claims.get("created_at"))
    trial_end = _parse_datetime(claims.get("trial_end"))
    if created is not None and trial_end is not None and trial_end > created:
        intervals.append((created, trial_end))

    history = claims.get("plan_history")
    if isinstance(history, Sequence) and not isinstance(history, (str, bytes)):
        for item in history:
            if not isinstance(item, Mapping):
                continue
            start = _parse_datetime(item.get("effective_start"))
            end = _parse_datetime(item.get("effective_end"))
            if start is not None and end is not None and end > start:
                intervals.append((start, end))

    # Legacy Premium records may predate plan_history. Keep a trial interval
    # as well; the Premium fallback is independent from the trial window.
    has_history_period = any(
        isinstance(item, Mapping)
        and _parse_datetime(item.get("effective_start")) is not None
        and _parse_datetime(item.get("effective_end")) is not None
        for item in history
    ) if isinstance(history, Sequence) and not isinstance(history, (str, bytes)) else False
    if not has_history_period and str(claims.get("plan") or "").lower() == "premium":
        start = _parse_datetime(claims.get("plan_updated_at")) or created
        end = _parse_datetime(claims.get("plan_end"))
        if start is not None and end is not None and end > start:
            intervals.append((start, end))

    return sorted(set(intervals), key=lambda item: item[0])


def _closed_during_entitlement(closed_at: Any, intervals: Sequence[tuple[datetime, datetime]]) -> bool:
    closed = _parse_datetime(closed_at)
    if closed is None:
        return False
    return any(start <= closed < end for start, end in intervals)


def personal_result_rows(
    repository: Any,
    *,
    user_id: str,
    claims: Mapping[str, Any],
    reset_at: datetime | None = None,
    is_admin: bool = False,
) -> list[Mapping[str, Any]]:
    """Join the user's delivery ledger to canonical outcomes without global leakage.

    A result is included only when its signal was delivered after the active global
    reset and it closed during one of that account's entitlement windows. This
    freezes accumulation during plan gaps, while a later renewal resumes the
    same personal record without admitting results closed during the gap.
    """
    delivery_reader = getattr(repository, "list_user_deliveries", None)
    result_reader = getattr(repository, "list_results_by_signal_ids", None)
    if not callable(delivery_reader) or not callable(result_reader):
        raise RepositoryUnavailable("El registro personal de señales no está disponible")

    deliveries = delivery_reader(str(user_id), start=reset_at)
    if reset_at is not None:
        deliveries = [
            row for row in deliveries
            if (_parse_datetime(row.get("delivered_at")) is not None
                and _parse_datetime(row.get("delivered_at")) >= reset_at)
        ]
    signal_ids = list(dict.fromkeys(
        str(row.get("signal_id") or "").strip() for row in deliveries
        if str(row.get("signal_id") or "").strip()
    ))
    if not signal_ids:
        return []

    results = result_reader(signal_ids)
    if is_admin:
        return list(results)

    intervals = entitlement_intervals(claims)
    if not intervals:
        return []

    delivered_at_by_id = {
        str(row.get("signal_id") or ""): _parse_datetime(row.get("delivered_at"))
        for row in deliveries
        if str(row.get("signal_id") or "")
    }
    eligible: list[Mapping[str, Any]] = []
    for row in results:
        signal_id = str(row.get("signal_id") or row.get("id") or "")
        delivered_at = delivered_at_by_id.get(signal_id)
        closed_at = _parse_datetime(row.get("closed_at"))
        # A closed signal opened from a symbol lookup after the fact was not an
        # operation delivered to the user while it was actionable.
        if delivered_at is None or closed_at is None or closed_at < delivered_at:
            continue
        if _closed_during_entitlement(closed_at, intervals):
            eligible.append(row)
    eligible.sort(key=lambda row: _parse_datetime(row.get("closed_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return eligible
