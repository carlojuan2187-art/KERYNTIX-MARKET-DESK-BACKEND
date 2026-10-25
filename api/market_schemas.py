"""API schemas for the existing web contract; unavailable analytics stay null."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Trend = Literal["up", "down", "flat"]


class QuoteTrend(BaseModel):
    model_config = ConfigDict(extra="allow")
    model_15m: Trend | None = Field(default=None, alias="15M")
    model_1h: Trend | None = Field(default=None, alias="1H")
    model_4h: Trend | None = Field(default=None, alias="4H")


class AssetQuote(BaseModel):
    model_config = ConfigDict(extra="allow")
    symbol: str
    name: str
    price: float = Field(gt=0)
    change_pct: float | None = None
    volume_24h: float | None = Field(default=None, ge=0)
    high_24h: float | None = Field(default=None, gt=0)
    low_24h: float | None = Field(default=None, gt=0)
    funding_rate: float | None = None
    open_interest: float | None = Field(default=None, ge=0)
    buy_pct: float | None = Field(default=None, ge=0, le=100)
    volume_ratio: float | None = Field(default=None, ge=0)
    score: float | None = None
    trend: QuoteTrend = Field(default_factory=QuoteTrend)
    sparkline: list[float] = Field(default_factory=list)
    liquidity_sweep: bool | None = None


class Candle(BaseModel):
    model_config = ConfigDict(extra="allow")
    time: str
    timestamp: int = Field(ge=0)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    cvd: float


class MarketFeedHealth(BaseModel):
    status: Literal["healthy", "degraded", "collecting", "unavailable"]
    source: Literal["websocket", "rest", "cache", "none"]
    connected: bool
    updated_at: str | None = None
    last_price_update_at: str | None = None
    stale_seconds: float | None = None
    message: str | None = None
