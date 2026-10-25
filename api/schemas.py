"""HTTP schemas that preserve the canonical signal and project legacy UI fields."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import AwareDatetime, Field, field_serializer, field_validator

from domain.signals import CanonicalModel, CanonicalSignal, SignalStatus, _as_utc, _utc_iso


class SignalEvent(CanonicalModel):
    """Event shape already consumed by the current React frontend."""
    type: Literal["new", "touched", "cancelled", "tp1", "tp2", "sl", "closed"]
    result: str | None = None
    r: float | None = None
    via: str | None = None


class SignalApiResponse(CanonicalSignal):
    """Rich canonical signal plus additive fields consumed by the existing UI.

    Inheriting CanonicalSignal intentionally keeps all strategy/scoring/lifecycle
    metadata in the response. The frontend can adopt those fields incrementally
    without the backend reducing the motor's output to the current mock shape.
    """
    status: SignalStatus
    entry_low: float = Field(gt=0)
    entry_high: float = Field(gt=0)
    tp1: float = Field(gt=0)
    tp2: float | None = Field(default=None, gt=0)
    tp3: float | None = Field(default=None, gt=0)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    leverage: float | None = Field(default=None, gt=0)
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    touched_at: AwareDatetime | None = None
    cancelled_at: AwareDatetime | None = None
    cancel_reason: str | None = None
    operation_result: Literal["OPEN", "TP1_HIT", "TP1", "TP2", "SL"] | None = None
    r_result: float | None = None
    event: SignalEvent | None = None

    @field_validator("created_at", "expires_at", "touched_at", "cancelled_at", mode="before")
    @classmethod
    def normalize_dates(cls, value: Any) -> Any:
        return _as_utc(value)

    @field_serializer("created_at", "expires_at", "touched_at", "cancelled_at", when_used="json")
    def serialize_dates(self, value: datetime | None) -> str | None:
        return _utc_iso(value)
