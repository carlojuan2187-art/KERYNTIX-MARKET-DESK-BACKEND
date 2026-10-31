"""Public request/response schemas for auth and account endpoints."""
from __future__ import annotations

import re
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PhoneChallengeRequest(StrictModel):
    phone: str
    ref_code: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("phone")
    @classmethod
    def valid_e164(cls, value: str) -> str:
        normalized = re.sub(r"\s+", "", value)
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", normalized):
            raise ValueError("phone debe estar en formato E.164")
        return normalized


class RegisterRequest(PhoneChallengeRequest):
    password: str = Field(min_length=10, max_length=128)
    language: Literal["en", "es"] = "en"


class PasswordLoginRequest(StrictModel):
    phone: str
    password: str = Field(min_length=1, max_length=128)

    @field_validator("phone")
    @classmethod
    def valid_e164(cls, value: str) -> str:
        normalized = re.sub(r"\s+", "", value)
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", normalized):
            raise ValueError("phone debe estar en formato E.164")
        return normalized


class SetPasswordRequest(StrictModel):
    password: str = Field(min_length=10, max_length=128)


class PhoneChallengeResponse(BaseModel):
    token: str
    phone: str
    step: Literal["awaiting_telegram"]
    expires_at: str
    resend_at: str


class TelegramCheckResponse(BaseModel):
    linked: bool
    telegram_chat_id: str | None = None


class VerifyCodeRequest(StrictModel):
    phone: str
    code: str = Field(pattern=r"^[0-9]{6}$")

    @field_validator("phone")
    @classmethod
    def valid_e164(cls, value: str) -> str:
        normalized = re.sub(r"\s+", "", value)
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", normalized):
            raise ValueError("phone debe estar en formato E.164")
        return normalized


class AuthResendRequest(StrictModel):
    token: str = Field(min_length=20, max_length=128)


class UserPatchRequest(StrictModel):
    language: Literal["en", "es"] | None = None
    exchange: str | None = Field(default=None, max_length=80)
    tz: str | None = Field(default=None, min_length=1, max_length=64)
    risk_profile: dict[str, float] | None = None

    @field_validator("risk_profile")
    @classmethod
    def validate_risk_profile(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is None:
            return value
        if set(value) != {"capital", "risk_pct"}:
            raise ValueError("risk_profile debe contener capital y risk_pct")
        capital = value["capital"]
        risk_pct = value["risk_pct"]
        if not (0 < capital <= 1_000_000_000):
            raise ValueError("capital debe ser mayor que 0")
        if not (0 < risk_pct <= 10):
            raise ValueError("risk_pct debe estar entre 0 y 10; 1 significa 1%")
        return value

    @field_validator("tz")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("tz debe ser una zona horaria IANA válida") from exc
        return value


class UserResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    user_id: str
    phone: str
    language: Literal["en", "es"] = "en"
    telegram_chat_id: str | None = None
    username: str | None = None
    plan: Literal["free", "premium"] = "free"
    trial_end: str | None = None
    plan_end: str | None = None
    ref_code: str
    referred_by: str | None = None
    ref_premium_valid: bool = False
    exchange: str | None = None
    risk_profile: dict[str, float]
    watchlist: list[str] = Field(default_factory=list, max_length=10)
    alert_prefs: dict[str, bool]
    tz: str
    banned: bool = False
    ban_until: str | None = None
    banned_at: str | None = None
    banned_by: str | None = None
    created_at: str
    last_activity: str
    is_admin: bool = False


class VerifyCodeResponse(BaseModel):
    access_token: str
    user: UserResponse
    is_new: bool
    # Only present when AUTH_REFRESH_IN_BODY is enabled (default). Lets the SPA
    # keep the session when the browser blocks the cross-site refresh cookie.
    refresh_token: str | None = None


class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str | None = Field(default=None, max_length=256)


class SessionRecordResponse(BaseModel):
    id: str
    device: str
    location: str
    current: bool
    last_activity: str


class TelegramLinkResponse(BaseModel):
    token: str
    deep_link: str
    expires_at: str
