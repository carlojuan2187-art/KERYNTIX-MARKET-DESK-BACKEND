from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import HTTPException

from api.security import verify_access_token


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def test_non_object_jwt_header_is_rejected_as_401(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "a-long-test-secret-that-is-not-empty")
    token = f"{_b64(json.dumps([]).encode())}.{_b64(json.dumps({}).encode())}.{_b64(b'bad-signature')}"
    with pytest.raises(HTTPException) as error:
        verify_access_token(token)
    assert error.value.status_code == 401
    assert error.value.detail["code"] == "UNAUTHENTICATED"


def test_malformed_access_token_ttl_configuration_is_a_service_error(monkeypatch):
    secret = "a-long-test-secret-that-is-not-empty"
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", secret)
    monkeypatch.setenv("ACCESS_TOKEN_MAX_TTL_SECONDS", "not-an-int")
    now = time.time()
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"sub": "user-1", "iat": now, "exp": now + 60}).encode())
    message = f"{header}.{claims}".encode("ascii")
    signature = _b64(hmac.new(secret.encode(), message, hashlib.sha256).digest())
    token = f"{header}.{claims}.{signature}"
    with pytest.raises(HTTPException) as error:
        verify_access_token(token)
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "AUTH_NOT_CONFIGURED"
