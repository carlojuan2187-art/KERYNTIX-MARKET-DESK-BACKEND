"""Validated input/output schemas for the risk calculator endpoint."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CalculatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    capital: float = Field(gt=0, allow_inf_nan=False)
    risk_pct: float = Field(ge=0.1, le=10.0, allow_inf_nan=False)
    entry: float = Field(gt=0, allow_inf_nan=False)
    stop: float = Field(gt=0, allow_inf_nan=False)
    tp1: float = Field(gt=0, allow_inf_nan=False)
    tp2: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    tp3: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    direction: Literal["LONG", "SHORT", "long", "short"] | None = None

    @model_validator(mode="after")
    def tp_order_slots(self) -> "CalculatorInput":
        if self.tp3 is not None and self.tp2 is None:
            raise ValueError("TP3 requiere que también se indique TP2.")
        if self.direction is not None:
            self.direction = self.direction.upper()  # type: ignore[assignment]
        return self


class CalculatorTarget(BaseModel):
    level: str
    price: float
    r: float
    usdt: float
    fees_usdt: float
    net_usdt: float


class CalculatorOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    risk_usdt: float
    qty: float
    position_size: float
    leverage_min: int
    isolated_margin: float
    liquidation_approx: float
    fee_taker_pct: float
    fees_usdt: float
    direction: Literal["LONG", "SHORT"]
    results: list[CalculatorTarget]
