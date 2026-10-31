"""Canonical FTrade signal/result schemas and legacy bot adapters.

Python target: 3.11+. Pydantic 2.x.
This module is deliberately independent from Telegram, MongoDB and market I/O.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any, Mapping, Sequence

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "1.0"


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStatus(str, Enum):
    PENDING = "PENDING"
    TOUCHED = "TOUCHED"
    CANCELLED = "CANCELLED"


class OutcomeState(str, Enum):
    OPEN = "OPEN"
    TP1_HIT = "TP1_HIT"
    CLOSED = "CLOSED"


class OutcomeResult(str, Enum):
    TP1 = "TP1"
    TP2 = "TP2"
    SL = "SL"
    INVALID = "INVALID"


class ValidClosedResult(str, Enum):
    TP1 = "TP1"
    TP2 = "TP2"
    SL = "SL"


class WinLoss(str, Enum):
    WIN = "win"
    LOSS = "loss"


class CanonicalModel(BaseModel):
    """Base policy: preserve unrecognized metadata instead of silently dropping it."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, validate_assignment=True, use_enum_values=True)


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # API datetimes are required to be aware; this fallback is defensive only.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_utc(value: Any) -> Any:
    """Normalize legacy naive datetimes as UTC; the Telegram bot stores datetime.utcnow()."""
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, str):
        parsed = value.strip()
        if parsed.endswith("Z"):
            parsed = parsed[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(parsed)
        except ValueError:
            return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return value


class EntryZone(CanonicalModel):
    low: float = Field(gt=0, allow_inf_nan=False)
    high: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_order(self) -> "EntryZone":
        if self.low > self.high:
            raise ValueError("entry_zone.low must be <= entry_zone.high")
        return self


class StrategyTarget(CanonicalModel):
    price: float = Field(gt=0, allow_inf_nan=False)
    rr: float | None = Field(default=None, allow_inf_nan=False)
    touches: int | None = Field(default=None, ge=0)
    source: str | None = None


class ScoreComponent(CanonicalModel):
    name: str = Field(min_length=1)
    points: float | None = Field(default=None, allow_inf_nan=False)
    value: float | None = Field(default=None, allow_inf_nan=False)
    passed: bool | None = None


class SignalScoring(CanonicalModel):
    raw_score: float | None = Field(default=None, allow_inf_nan=False)
    normalized_score: float | None = Field(default=None, allow_inf_nan=False)
    profile: str | None = None
    calibration: str | None = None
    components: list[ScoreComponent] = Field(default_factory=list)
    raw_components: list[ScoreComponent] | None = None
    normalized_components: list[ScoreComponent] | None = None


class PostFillInvalidation(CanonicalModel):
    minutes: int | None = Field(default=None, ge=0)
    min_tp1_progress_pct: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    reason: str | None = None


class StrategyRuntimePolicy(CanonicalModel):
    post_fill_invalidation: PostFillInvalidation | None = None


class StrategyMetadata(CanonicalModel):
    setup_group: str | None = None
    delivery_plan: str | None = None
    filter_profile: str | None = None
    setup_stage: str | None = None
    send_mode: str | None = None
    entry_model: str | None = None
    entry_model_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    entry_sent_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    execution_timeframe: str | None = None
    setup_confirmed_at: AwareDatetime | None = None
    atr_pct: float | None = Field(default=None, allow_inf_nan=False)
    atr_value: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    risk_atr: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    target_model: str | None = None
    tp1_rr: float | None = Field(default=None, allow_inf_nan=False)
    preentry_distance_atr: float | None = Field(default=None, allow_inf_nan=False)
    bars_after_sweep: int | None = Field(default=None, ge=0)
    sweep_to_market_atr: float | None = Field(default=None, allow_inf_nan=False)
    liquidity_zone: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    liquidity_sweep_distance_atr: float | None = Field(default=None, allow_inf_nan=False)
    liquidity_sweep_extreme_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    higher_tf_context: dict[str, Any] | None = None
    runtime: StrategyRuntimePolicy | dict[str, Any] | None = None

    @field_validator("setup_confirmed_at", mode="before")
    @classmethod
    def normalize_setup_confirmed_at(cls, value: Any) -> Any:
        return _as_utc(value)

    @field_serializer("setup_confirmed_at", when_used="json")
    def serialize_setup_confirmed_at(self, value: datetime | None) -> str | None:
        return _utc_iso(value)


class EstimatedEntryMinutes(CanonicalModel):
    min: int = Field(ge=0)
    max: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self) -> "EstimatedEntryMinutes":
        if self.min > self.max:
            raise ValueError("estimated_entry_minutes.min must be <= max")
        return self


class SignalLifecycle(CanonicalModel):
    status: SignalStatus
    created_at: AwareDatetime
    pending_at: AwareDatetime | None = None
    status_updated_at: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    telegram_valid_until: AwareDatetime | None = None
    entry_expires_at: AwareDatetime | None = None
    display_until: AwareDatetime | None = None
    estimated_entry_minutes: EstimatedEntryMinutes | None = None
    touched_at: AwareDatetime | None = None
    touch_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    cancelled_at: AwareDatetime | None = None
    cancel_reason: str | None = None

    @field_validator(
        "created_at", "pending_at", "status_updated_at", "valid_until",
        "telegram_valid_until", "entry_expires_at", "display_until",
        "touched_at", "cancelled_at", mode="before"
    )
    @classmethod
    def normalize_dates(cls, value: Any) -> Any:
        return _as_utc(value)

    @field_serializer(
        "created_at", "pending_at", "status_updated_at", "valid_until",
        "telegram_valid_until", "entry_expires_at", "display_until",
        "touched_at", "cancelled_at", when_used="json"
    )
    def serialize_dates(self, value: datetime | None) -> str | None:
        return _utc_iso(value)


class SignalOutcomeProgress(CanonicalModel):
    state: OutcomeState | None = None
    result: OutcomeResult | None = None
    tracked_until: AwareDatetime | None = None
    tp1_at: AwareDatetime | None = None
    tp2_at: AwareDatetime | None = None
    protection_armed_at: AwareDatetime | None = None
    last_live_price_at: AwareDatetime | None = None
    protective_stop_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    exit_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    exit_via: str | None = None
    calculation_version: str | None = None
    execution_model: str | None = None
    closed_at: AwareDatetime | None = None

    @field_validator("tracked_until", "tp1_at", "tp2_at", "protection_armed_at", "last_live_price_at", "closed_at", mode="before")
    @classmethod
    def normalize_dates(cls, value: Any) -> Any:
        return _as_utc(value)

    @field_serializer("tracked_until", "tp1_at", "tp2_at", "protection_armed_at", "last_live_price_at", "closed_at", when_used="json")
    def serialize_dates(self, value: datetime | None) -> str | None:
        return _utc_iso(value)

    @model_validator(mode="after")
    def state_result_consistency(self) -> "SignalOutcomeProgress":
        if self.result is not None and self.state != OutcomeState.CLOSED:
            raise ValueError("outcome.result solo puede existir cuando outcome.state=CLOSED")
        if self.state == OutcomeState.CLOSED and self.result is None:
            raise ValueError("outcome.result es obligatorio cuando outcome.state=CLOSED")
        return self


class SignalTradeMetrics(CanonicalModel):
    modeled_entry_fill: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    modeled_risk_distance: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    r_by_target_from_modeled_fill: list[float | None] = Field(default_factory=list)


class CanonicalSignal(CanonicalModel):
    schema_version: str = SCHEMA_VERSION
    id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    direction: Direction
    visibility: str = Field(min_length=1)
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    entry_zone: EntryZone
    stop_loss: float = Field(gt=0, allow_inf_nan=False)
    take_profits: list[float] = Field(min_length=1)
    targets: list[StrategyTarget] = Field(default_factory=list)
    timeframes: list[str] = Field(default_factory=list)
    score: float | None = Field(default=None, allow_inf_nan=False)
    scoring: SignalScoring = Field(default_factory=SignalScoring)
    strategy: StrategyMetadata = Field(default_factory=StrategyMetadata)
    lifecycle: SignalLifecycle
    outcome: SignalOutcomeProgress = Field(default_factory=SignalOutcomeProgress)
    trade_metrics: SignalTradeMetrics | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("take_profits", mode="before")
    @classmethod
    def positive_finite_targets(cls, values: Any) -> Any:
        if values is None:
            return values
        for value in values:
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("cada take profit debe ser numérico") from exc
            if number <= 0 or not isfinite(number):
                raise ValueError("cada take profit debe ser positivo y finito")
        return values

    @field_validator("timeframes", mode="before")
    @classmethod
    def normalize_timeframes(cls, values: Any) -> Any:
        if values is None:
            return []
        if not isinstance(values, (list, tuple)):
            raise ValueError("timeframes debe ser una lista")
        return [str(value).strip().upper() for value in values]

    @model_validator(mode="after")
    def version_is_supported(self) -> "CanonicalSignal":
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"schema_version no soportada: {self.schema_version}")
        return self


class CanonicalSignalResult(CanonicalModel):
    schema_version: str = SCHEMA_VERSION
    id: str = Field(min_length=1)
    signal_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    direction: Direction
    visibility: str | None = None
    entry_fill: float = Field(gt=0, allow_inf_nan=False)
    entry_zone: EntryZone
    stop_loss: float = Field(gt=0, allow_inf_nan=False)
    take_profits: list[float] = Field(min_length=1)
    risk: float = Field(gt=0, allow_inf_nan=False)
    r_tp1: float = Field(ge=0, allow_inf_nan=False)
    r_tp2: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    result: ValidClosedResult
    outcome: WinLoss
    r_real: float = Field(allow_inf_nan=False)
    ambiguous: bool
    score: float | None = Field(default=None, allow_inf_nan=False)
    created_at: AwareDatetime | None = None
    touched_at: AwareDatetime | None = None
    tp1_at: AwareDatetime | None = None
    tp2_at: AwareDatetime | None = None
    entry_reference: str | None = None
    exit_price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    exit_via: str | None = None
    calculation_version: str | None = None
    execution_model: str | None = None
    closed_at: AwareDatetime
    evaluated_at: AwareDatetime
    duration_minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    signal_snapshot: dict[str, Any] | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("created_at", "touched_at", "tp1_at", "tp2_at", "closed_at", "evaluated_at", mode="before")
    @classmethod
    def normalize_dates(cls, value: Any) -> Any:
        return _as_utc(value)

    @field_serializer("created_at", "touched_at", "tp1_at", "tp2_at", "closed_at", "evaluated_at", when_used="json")
    def serialize_dates(self, value: datetime | None) -> str | None:
        return _utc_iso(value)

    @model_validator(mode="after")
    def result_consistency(self) -> "CanonicalSignalResult":
        expected = WinLoss.WIN if self.result in (ValidClosedResult.TP1, ValidClosedResult.TP2) else WinLoss.LOSS
        if self.outcome != expected:
            raise ValueError(f"outcome debe ser {expected.value!r} para result={self.result!r}")
        if self.risk <= 0:
            raise ValueError("risk debe ser positivo")
        return self


# Keys owned by the canonical model; all remaining top-level legacy signal keys
# are captured under strategy metadata so a migration does not silently discard them.
_ROOT_KEYS = {
    "_id", "id", "signal_id", "schema_version", "symbol", "direction", "visibility",
    "entry_price", "entry_zone", "stop_loss", "take_profits", "targets", "timeframes",
    "score", "components", "created_at", "status", "status_updated_at",
    "pending_at", "valid_until", "telegram_valid_until", "entry_expires_at", "display_until",
    "estimated_entry_minutes", "touched_at", "touch_price", "cancelled_at", "cancel_reason",
    "outcome", "trade_metrics", "scoring", "strategy", "lifecycle", "result",
    "raw_score", "normalized_score", "raw_components", "normalized_components",
    "score_profile", "score_calibration", "runtime", "strategy_runtime",
}


def _components(values: Any) -> list[dict[str, Any]]:
    """Normalize known score components but preserve unknown component payloads."""
    if not isinstance(values, (list, tuple)):
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(values):
        if isinstance(item, Mapping):
            data = dict(item)
            # Don't invent numeric scoring values if a legacy record lacks them.
            data.setdefault("name", str(data.get("key") or data.get("id") or f"component_{index + 1}"))
            data.setdefault("points", None)
            data.setdefault("value", None)
            data.setdefault("passed", None)
            normalized.append(data)
        elif isinstance(item, str):
            normalized.append({"name": item, "points": None, "value": None, "passed": None})
        else:
            normalized.append({"name": f"component_{index + 1}", "points": None, "value": None, "passed": None, "legacy_value": item})
    return normalized


def adapt_bot_signal(document: Mapping[str, Any], *, include_trade_metrics: bool = True) -> CanonicalSignal:
    """Normalize a legacy Mongo `signals` document into the canonical API schema.

    The adapter never manufactures strategy values. It applies only compatibility
    defaults already present in the Telegram bot: inferred entry zone for old docs,
    and PENDING for pre-lifecycle records that had no status field.
    """
    raw = dict(document)
    raw_id = raw.get("id") or raw.get("_id") or raw.get("signal_id")
    if raw_id is None or str(raw_id).strip() == "":
        raise ValueError("la señal histórica necesita id/_id/signal_id para normalizarse")

    symbol = raw.get("symbol")
    direction = str(raw.get("direction") or "").upper()
    entry = raw.get("entry_price")
    if entry is None:
        raise ValueError("la señal histórica necesita entry_price")

    zone = raw.get("entry_zone")
    if not isinstance(zone, Mapping) or zone.get("low") is None or zone.get("high") is None:
        # Mirrors app.signals.calculate_entry_zone(entry, pct=0.0015) for legacy callers.
        entry_number = float(entry)
        zone = {"low": round(entry_number * (1 - 0.0015), 4), "high": round(entry_number * (1 + 0.0015), 4)}
    else:
        low, high = float(zone["low"]), float(zone["high"])
        zone = {"low": min(low, high), "high": max(low, high)}

    lifecycle_source = dict(raw.get("lifecycle") or {})
    for key in (
        "status", "created_at", "pending_at", "status_updated_at", "valid_until",
        "telegram_valid_until", "entry_expires_at", "display_until", "estimated_entry_minutes",
        "touched_at", "touch_price", "cancelled_at", "cancel_reason",
    ):
        if key in raw and key not in lifecycle_source:
            lifecycle_source[key] = raw[key]
    lifecycle_source.setdefault("status", "PENDING")
    lifecycle_source["status"] = str(lifecycle_source["status"]).upper()
    if "created_at" not in lifecycle_source:
        raise ValueError("la señal histórica necesita created_at")

    scoring_source = dict(raw.get("scoring") or {})
    aliases = {
        "raw_score": "raw_score",
        "normalized_score": "normalized_score",
        "score_profile": "profile",
        "score_calibration": "calibration",
        "raw_components": "raw_components",
        "normalized_components": "normalized_components",
        "components": "components",
    }
    for source_key, destination_key in aliases.items():
        if source_key in raw and destination_key not in scoring_source:
            scoring_source[destination_key] = raw[source_key]
    if "components" not in scoring_source:
        scoring_source["components"] = raw.get("components") or []
    for key in ("components", "raw_components", "normalized_components"):
        if scoring_source.get(key) is not None:
            scoring_source[key] = _components(scoring_source[key])

    strategy_source = dict(raw.get("strategy") or {})
    # Support early draft names while keeping the canonical nested names stable.
    if "runtime" not in strategy_source:
        if raw.get("runtime") is not None:
            strategy_source["runtime"] = raw["runtime"]
        elif raw.get("strategy_runtime") is not None:
            strategy_source["runtime"] = raw["strategy_runtime"]
    for key in (
        "setup_group", "delivery_plan", "filter_profile", "setup_stage", "send_mode",
        "entry_model", "entry_model_price", "entry_sent_price", "execution_timeframe",
        "setup_confirmed_at", "atr_pct", "atr_value", "risk_atr", "target_model",
        "tp1_rr", "preentry_distance_atr", "bars_after_sweep", "sweep_to_market_atr",
        "liquidity_zone", "liquidity_sweep_distance_atr", "liquidity_sweep_extreme_price",
        "higher_tf_context",
    ):
        if key in raw and key not in strategy_source:
            strategy_source[key] = raw[key]
    for key, value in raw.items():
        if key not in _ROOT_KEYS and key not in strategy_source:
            strategy_source[key] = value
    strategy_source.pop("targets", None)  # targets are a top-level canonical collection.

    targets_source = raw.get("targets")
    if targets_source is None:
        targets_source = (raw.get("strategy") or {}).get("targets", []) if isinstance(raw.get("strategy"), Mapping) else []
    targets_source = targets_source or []

    outcome_source = dict(raw.get("outcome") or {})
    # Legacy DB docs might store a result field flat; only retain it if its state is CLOSED.
    if "result" in raw and "result" not in outcome_source and str(outcome_source.get("state") or "").upper() == "CLOSED":
        outcome_source["result"] = raw["result"]

    canonical_data = {
        "schema_version": raw.get("schema_version", SCHEMA_VERSION),
        "id": str(raw_id),
        "symbol": symbol,
        "direction": direction,
        "visibility": raw.get("visibility", "premium"),
        "entry_price": entry,
        "entry_zone": zone,
        "stop_loss": raw.get("stop_loss"),
        "take_profits": raw.get("take_profits") or [],
        "targets": targets_source,
        "timeframes": raw.get("timeframes") or [],
        "score": raw.get("score"),
        "scoring": scoring_source,
        "strategy": strategy_source,
        "lifecycle": lifecycle_source,
        "outcome": outcome_source,
    }
    # Keep extra canonical sections if the input already contains them.
    if raw.get("trade_metrics") is not None:
        canonical_data["trade_metrics"] = raw["trade_metrics"]

    signal = CanonicalSignal.model_validate(canonical_data)
    if include_trade_metrics and signal.lifecycle.status == SignalStatus.TOUCHED:
        levels = compute_trade_levels(signal)
        if levels is not None:
            signal.trade_metrics = SignalTradeMetrics(
                modeled_entry_fill=levels["fill"],
                modeled_risk_distance=levels["risk"],
                r_by_target_from_modeled_fill=[levels["r_tp1"]] + ([levels["r_tp2"]] if levels["r_tp2"] is not None else []),
            )
    return signal


def _signal_mapping(signal: CanonicalSignal | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(signal, CanonicalSignal):
        return signal.model_dump(mode="python")
    return dict(signal)


def lifecycle_geometry_issues(signal: CanonicalSignal | Mapping[str, Any]) -> list[str]:
    """Mirror app.signal_lifecycle._valid_geometry; does not mutate or reject records."""
    data = _signal_mapping(signal)
    direction = str(data.get("direction") or "").upper()
    zone = data.get("entry_zone") or {}
    if hasattr(zone, "model_dump"):
        zone = zone.model_dump()
    try:
        low = float(zone.get("low", data.get("entry_price") or 0))
        high = float(zone.get("high", data.get("entry_price") or 0))
        sl = float(data.get("stop_loss") or 0)
        tps = list(data.get("take_profits") or [])
        tp1 = float(tps[0]) if tps else 0.0
    except (TypeError, ValueError):
        return ["NON_NUMERIC_GEOMETRY"]
    if low > high:
        low, high = high, low
    if low <= 0 or high <= 0 or sl <= 0 or tp1 <= 0:
        return ["NON_POSITIVE_GEOMETRY"]
    if direction == "LONG":
        if not sl < low:
            return ["STOP_LOSS_NOT_BELOW_ENTRY_ZONE"]
        if not tp1 > high:
            return ["TP1_NOT_ABOVE_ENTRY_ZONE"]
    elif direction == "SHORT":
        if not sl > high:
            return ["STOP_LOSS_NOT_ABOVE_ENTRY_ZONE"]
        if not tp1 < low:
            return ["TP1_NOT_BELOW_ENTRY_ZONE"]
    else:
        return ["INVALID_DIRECTION"]
    return []


def compute_trade_levels(signal: CanonicalSignal | Mapping[str, Any]) -> dict[str, Any] | None:
    """Mirror engine.outcome_logic.trade_levels using canonical strategy entry.

    The entry zone is a trigger window only. R metrics use entry_price, the same
    modeled price used by the strategy to calculate SL, TP1 and TP2.
    """
    data = _signal_mapping(signal)
    direction = str(data.get("direction") or "").upper()
    if direction not in ("LONG", "SHORT"):
        return None
    zone = data.get("entry_zone") or {}
    if hasattr(zone, "model_dump"):
        zone = zone.model_dump()
    try:
        entry = float(data.get("entry_price") or 0)
        low = float(zone.get("low", entry))
        high = float(zone.get("high", entry))
        if low > high:
            low, high = high, low
        sl = float(data.get("stop_loss") or 0)
        tps = [float(value) for value in (data.get("take_profits") or []) if float(value) > 0][:2]
    except (TypeError, ValueError, OverflowError):
        return None
    if entry <= 0 or low <= 0 or high <= 0 or not (low <= entry <= high) or sl <= 0 or not tps:
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


def adapt_bot_result(document: Mapping[str, Any]) -> CanonicalSignalResult:
    """Normalize an existing `signal_results` row to the public result contract."""
    raw = dict(document)
    signal_id = raw.get("signal_id")
    if not signal_id:
        raise ValueError("el resultado histórico necesita signal_id")
    result = str(raw.get("result") or "").upper()
    now_fallback = raw.get("closed_at") or raw.get("evaluated_at")
    result_id = raw.get("id") or raw.get("_id") or f"result:{signal_id}"
    result_data = {
        "schema_version": raw.get("schema_version", SCHEMA_VERSION),
        "id": str(result_id),
        "signal_id": str(signal_id),
        "symbol": raw.get("symbol"),
        "direction": str(raw.get("direction") or "").upper(),
        "visibility": raw.get("visibility"),
        "entry_fill": raw.get("entry_fill"),
        "entry_zone": raw.get("entry_zone"),
        "stop_loss": raw.get("stop_loss"),
        "take_profits": raw.get("take_profits") or [],
        "risk": raw.get("risk"),
        "r_tp1": raw.get("r_tp1"),
        "r_tp2": raw.get("r_tp2"),
        "result": result,
        "outcome": str(raw.get("outcome") or ("win" if result in ("TP1", "TP2") else "loss")).lower(),
        "r_real": raw.get("r_real"),
        "ambiguous": bool(raw.get("ambiguous", False)),
        "score": raw.get("score"),
        "created_at": raw.get("created_at"),
        "touched_at": raw.get("touched_at"),
        "tp1_at": raw.get("tp1_at"),
        "tp2_at": raw.get("tp2_at"),
        "entry_reference": raw.get("entry_reference"),
        "exit_price": raw.get("exit_price"),
        "exit_via": raw.get("exit_via"),
        "calculation_version": raw.get("calculation_version"),
        "execution_model": raw.get("execution_model"),
        "closed_at": raw.get("closed_at") or now_fallback,
        "evaluated_at": raw.get("evaluated_at") or now_fallback,
        "duration_minutes": raw.get("duration_minutes"),
        "signal_snapshot": raw.get("signal_snapshot"),
    }
    return CanonicalSignalResult.model_validate(result_data)
