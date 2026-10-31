"""Strict request/response contracts for browser Web Push subscriptions."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


_ALLOWED_HOST_SUFFIXES = (
    ".push.services.mozilla.com",
    ".push.apple.com",
    ".notify.windows.com",
    ".push.microsoft.com",
)
_ALLOWED_EXACT_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
_B64URL = re.compile(r"^[A-Za-z0-9_=-]+$")


def validate_push_endpoint(value: str) -> str:
    endpoint = str(value).strip()
    if len(endpoint) > 2048:
        raise ValueError("endpoint excede el tamaño permitido")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("endpoint debe ser una URL HTTPS segura")
    if parsed.port not in (None, 443):
        raise ValueError("endpoint no admite puertos personalizados")
    host = parsed.hostname.lower().rstrip(".")
    if not (host in _ALLOWED_EXACT_HOSTS or any(host.endswith(suffix) for suffix in _ALLOWED_HOST_SUFFIXES)):
        raise ValueError("endpoint no pertenece a un servicio de Web Push admitido")
    if not parsed.path or parsed.path == "/":
        raise ValueError("endpoint no parece una URL de suscripción push")
    return endpoint


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PushKeys(StrictModel):
    p256dh: str = Field(min_length=32, max_length=256)
    auth: str = Field(min_length=8, max_length=128)

    @field_validator("p256dh", "auth")
    @classmethod
    def validate_base64url(cls, value: str) -> str:
        compact = value.strip()
        if not _B64URL.fullmatch(compact):
            raise ValueError("la clave debe usar base64url")
        return compact


class WebPushSubscriptionCreate(StrictModel):
    endpoint: str = Field(min_length=40, max_length=2048)
    expiration_time: int | float | None = Field(default=None, alias="expirationTime")
    keys: PushKeys

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return validate_push_endpoint(value)

    @field_validator("expiration_time")
    @classmethod
    def normalize_expiration(cls, value: int | float | None) -> int | None:
        if value is None:
            return None
        numeric = float(value)
        if numeric < 0 or numeric > 10**13:
            raise ValueError("expirationTime no es válido")
        return int(numeric)


class WebPushSubscriptionResponse(BaseModel):
    id: str
    endpoint_origin: str
    user_agent: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class WebPushSubscriptionListResponse(BaseModel):
    items: list[WebPushSubscriptionResponse]
    total: int
    max_items: int


class WebPushSubscriptionDelete(StrictModel):
    endpoint: str = Field(min_length=40, max_length=2048)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return validate_push_endpoint(value)
