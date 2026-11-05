"""Read service and projection from canonical signal data to the current UI contract."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from pydantic import ValidationError

from api.repositories.signals import RepositoryUnavailable, SignalRepository
from api.schemas import SignalApiResponse, SignalEvent
from domain.signals import (
    CanonicalSignal,
    OutcomeResult,
    OutcomeState,
    SignalStatus,
    adapt_bot_signal,
)

logger = logging.getLogger(__name__)


class SignalDataIntegrityError(RuntimeError):
    """One stored record cannot be represented faithfully by the canonical contract."""


def _strategy_value(signal: CanonicalSignal, key: str) -> Any:
    if key in (signal.strategy.model_extra or {}):
        return signal.strategy.model_extra[key]
    return getattr(signal.strategy, key, None)


def _operation_result(signal: CanonicalSignal) -> str | None:
    # Preserve a legacy top-level value if the source has one, but validate it against
    # the exact result states supported by the web contract.
    direct = _strategy_value(signal, "operation_result")
    allowed = {"OPEN", "TP1_HIT", "TP1", "TP2", "SL"}
    if isinstance(direct, str) and direct.upper() in allowed:
        return direct.upper()
    state = signal.outcome.state
    result = signal.outcome.result
    if state == OutcomeState.OPEN:
        return "OPEN"
    if state == OutcomeState.TP1_HIT:
        return "TP1_HIT"
    if state == OutcomeState.CLOSED and result in (OutcomeResult.TP1, OutcomeResult.TP2, OutcomeResult.SL):
        return str(result)
    return None


def _score_breakdown(signal: CanonicalSignal) -> dict[str, float]:
    components = signal.scoring.normalized_components or signal.scoring.components
    out: dict[str, float] = {}
    for component in components:
        if component.points is not None:
            out[component.name] = float(component.points)
    # Preserve a legacy score_breakdown map when available; structured components
    # above take precedence to avoid flattening richer engine data.
    legacy = _strategy_value(signal, "score_breakdown")
    if isinstance(legacy, Mapping):
        for key, value in legacy.items():
            if key not in out:
                try:
                    out[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
    return out


def to_api_signal(signal: CanonicalSignal) -> SignalApiResponse:
    """Expose all canonical fields and additive aliases used by the current React UI."""
    life = signal.lifecycle
    outcome = signal.outcome
    targets = list(signal.take_profits)
    operation_result = _operation_result(signal)
    r_result = _strategy_value(signal, "r_result")
    if r_result is None:
        r_result = _strategy_value(signal, "r_real")
    try:
        r_value = float(r_result) if r_result is not None else None
    except (TypeError, ValueError):
        r_value = None

    event: SignalEvent | None = None
    event_result: str | None = None
    event_type: str | None = None
    if outcome.state == OutcomeState.CLOSED:
        if operation_result in ("TP1", "TP2", "SL"):
            event_type = "closed"
            event_result = operation_result
        # INVALID remains in the canonical outcome. The current UI contract has no
        # matching operation_result/event result, so don't mislabel it as "touched".
    elif operation_result == "TP1_HIT":
        event_type = "tp1"
        event_result = "TP1"
    elif life.status == SignalStatus.CANCELLED:
        event_type = "cancelled"
    elif life.status == SignalStatus.TOUCHED:
        event_type = "touched"
    else:
        event_type = "new"
    if event_type:
        event = SignalEvent(type=event_type, result=event_result, r=r_value)

    data = signal.model_dump(mode="python")
    data.update({
        "status": life.status,
        "entry_low": signal.entry_zone.low,
        "entry_high": signal.entry_zone.high,
        "tp1": targets[0] if len(targets) > 0 else None,
        "tp2": targets[1] if len(targets) > 1 else None,
        "tp3": targets[2] if len(targets) > 2 else None,
        "score_breakdown": _score_breakdown(signal),
        "leverage": _safe_positive(_strategy_value(signal, "leverage")),
        "created_at": life.created_at,
        "expires_at": life.entry_expires_at or life.valid_until,
        "touched_at": life.touched_at,
        "cancelled_at": life.cancelled_at,
        "cancel_reason": life.cancel_reason,
        "operation_result": operation_result,
        "r_result": r_value,
        "event": event,
    })
    return SignalApiResponse.model_validate(data)


def _safe_positive(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number > 0 else None
    except (TypeError, ValueError):
        return None


class SignalService:
    def __init__(self, repository: SignalRepository, *, cancelled_display_minutes: int = 30) -> None:
        self.repository = repository
        self.cancelled_display_minutes = max(5, cancelled_display_minutes)

    @staticmethod
    def _canonical(raw: Mapping[str, Any]) -> CanonicalSignal:
        record_id = str(raw.get("_id") or raw.get("id") or raw.get("signal_id") or "desconocido")
        try:
            return adapt_bot_signal(raw, include_trade_metrics=True)
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            logger.exception("Señal no válida en almacenamiento; id=%s", record_id)
            raise SignalDataIntegrityError(f"El registro de señal {record_id} no cumple el contrato canónico") from exc

    def list_visible(self, *, limit: int = 10) -> list[SignalApiResponse]:
        bounded_limit = max(1, min(limit, 10))
        # Prefer the repository's indexed active-state query. Closed history is
        # not part of the live Signals screen and must not be scanned on every visit.
        active_reader = getattr(self.repository, "list_active_signals", None)
        if callable(active_reader):
            raw_docs = active_reader(limit=max(80, bounded_limit * 8))
        else:
            raw_docs = self.repository.list_signals(limit=100)
        now = datetime.now(timezone.utc)
        cancelled_cutoff = now - timedelta(minutes=self.cancelled_display_minutes)
        output: list[SignalApiResponse] = []
        for raw in raw_docs:
            signal = self._canonical(raw)
            projected = to_api_signal(signal)
            # Closed INVALID records remain auditable in storage but are no longer
            # active pending/touched/cancelled cards in the live signals feed.
            if signal.outcome.state == OutcomeState.CLOSED or projected.operation_result in {"TP1", "TP2", "SL"}:
                continue
            if projected.status == SignalStatus.PENDING and projected.expires_at is not None and projected.expires_at <= now:
                continue
            if projected.status == SignalStatus.CANCELLED:
                cancelled_at = projected.cancelled_at or projected.created_at
                if cancelled_at < cancelled_cutoff:
                    continue
            output.append(projected)
            if len(output) >= bounded_limit:
                break
        return output

    def latest_for_symbol(self, symbol: str) -> SignalApiResponse | None:
        normalized = symbol.strip().upper()
        if not normalized or len(normalized) > 40:
            return None
        raw = self.repository.latest_by_symbol(normalized)
        if raw is None:
            return None
        return to_api_signal(self._canonical(raw))
