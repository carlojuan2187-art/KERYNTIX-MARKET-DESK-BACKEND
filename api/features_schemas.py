"""Validation schemas for watchlist, user alerts and notification inbox."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.watchlist import normalize_symbol


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WatchlistAddRequest(StrictModel):
    symbol: str = Field(min_length=2, max_length=28)

    @field_validator("symbol")
    @classmethod
    def normalize(cls, value: str) -> str:
        try:
            return normalize_symbol(value)
        except ValueError as exc:
            raise ValueError("symbol debe ser un perpetuo USDT válido") from exc


class AlertPreferencesPatch(StrictModel):
    on: bool | None = None
    liq: bool | None = None
    mov: bool | None = None
    vol: bool | None = None
    signals: bool | None = None
    price: bool | None = None
    plans: bool | None = None
    in_app: bool | None = None
    telegram: bool | None = None
    web_push: bool | None = None


class PriceAlertCreate(StrictModel):
    symbol: str = Field(min_length=2, max_length=28)
    condition: Literal["above", "below"]
    target_price: float = Field(gt=0, le=1_000_000_000)
    repeat: bool = False
    cooldown_seconds: int = Field(default=300, ge=60, le=86400)

    @field_validator("symbol")
    @classmethod
    def normalize(cls, value: str) -> str:
        try:
            return normalize_symbol(value)
        except ValueError as exc:
            raise ValueError("symbol debe ser un perpetuo USDT válido") from exc


class PriceAlertPatch(StrictModel):
    enabled: bool | None = None
    repeat: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=60, le=86400)


class NotificationReadRequest(StrictModel):
    read: bool = True


class NotificationResponse(BaseModel):
    id: str
    event_id: str
    kind: str
    title: str
    message: str
    symbol: str | None = None
    metadata: dict = Field(default_factory=dict)
    created_at: str | None = None
    read_at: str | None = None


class NotificationListResponse(BaseModel):
    items: list[dict]
    unread_count: int
    limit: int
    next_before: str | None = None


class NotificationReadAllResponse(BaseModel):
    updated: int
    unread_count: int


class WatchlistResponse(BaseModel):
    items: list[dict]
    total: int
    max_items: int
    status: str
    source: str
    updated_at: str | None = None


class PriceAlertResponse(BaseModel):
    id: str
    user_id: str
    symbol: str
    condition: Literal["above", "below"]
    target_price: float
    enabled: bool
    repeat: bool
    cooldown_seconds: int
    created_at: str | None = None
    updated_at: str | None = None
    triggered_at: str | None = None
    last_triggered_at: str | None = None
    trigger_count: int = 0
