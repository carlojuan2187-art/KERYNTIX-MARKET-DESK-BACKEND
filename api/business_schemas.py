"""Strict schemas for billing, referrals, support, and administrative routes."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictBusinessModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PaymentVerifyRequest(StrictBusinessModel):
    sender_address: str = Field(min_length=42, max_length=42, pattern=r"^0x[a-fA-F0-9]{40}$")


class AdminPremiumRequest(StrictBusinessModel):
    days: int = Field(ge=1, le=365)
    reason: str = Field(min_length=5, max_length=500)
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 5:
            raise ValueError("reason must contain at least 5 non-space characters")
        return value


class AdminBanRequest(StrictBusinessModel):
    days: int | None = Field(default=None, ge=1, le=365)
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 5:
            raise ValueError("reason must contain at least 5 non-space characters")
        return value


class AdminUnbanRequest(StrictBusinessModel):
    reason: str = Field(min_length=5, max_length=500)

    @field_validator("reason")
    @classmethod
    def clean_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 5:
            raise ValueError("reason must contain at least 5 non-space characters")
        return value


class SupportCreateRequest(StrictBusinessModel):
    subject: str = Field(min_length=5, max_length=120)
    message: str = Field(min_length=10, max_length=5000)


class SupportMessageRequest(StrictBusinessModel):
    message: str = Field(min_length=2, max_length=5000)


class AdminSupportReplyRequest(StrictBusinessModel):
    message: str = Field(min_length=2, max_length=5000)
    close: bool = False
    status: Literal["waiting_user", "in_progress", "closed"] | None = None
