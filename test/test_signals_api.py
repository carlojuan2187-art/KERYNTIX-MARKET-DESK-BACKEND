from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.repositories.signals import InMemorySignalRepository
from api.security import Principal, current_principal, require_analytical_access
from main import create_app


def iso(offset_minutes: int = 0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=offset_minutes)


def raw_signal(
    *,
    symbol: str = "BTCUSDT",
    created_offset: int = 0,
    status: str = "PENDING",
    operation_state: str = "OPEN",
    result: str | None = None,
    cancel_offset: int | None = None,
) -> dict:
    now = iso(created_offset)
    return {
        "_id": f"legacy-{symbol}-{created_offset}-{status}-{operation_state}",
        "symbol": symbol,
        "direction": "LONG",
        "visibility": "premium",
        "entry_price": 100.0,
        "entry_zone": {"low": 99.5, "high": 100.5},
        "stop_loss": 98.0,
        "take_profits": [104.0, 108.0, 112.0],
        "timeframes": ["15M", "1H"],
        "score": 84,
        "components": ["momentum", {"name": "liquidity", "points": 12, "value": 0.8, "passed": True}],
        "raw_score": 84,
        "normalized_score": 86,
        "score_breakdown": {"legacy_model": 8},
        "leverage": 5,
        "created_at": now,
        "entry_expires_at": iso(15),
        "status": status,
        "touched_at": iso(-2) if status == "TOUCHED" else None,
        "touch_price": 100.2 if status == "TOUCHED" else None,
        "cancelled_at": iso(cancel_offset) if cancel_offset is not None else None,
        "cancel_reason": "ENTRY_NOT_TOUCHED" if status == "CANCELLED" else None,
        "outcome": {"state": operation_state, **({"result": result} if result else {})},
        "setup_group": "liquidity_sweep",
        "atr_pct": 1.25,
        "liquidity_zone": 99.8,
        "custom_motor_field": {"preserve": True},
    }


@pytest.fixture
def client():
    docs = [
        raw_signal(symbol="BTCUSDT", created_offset=-1),
        raw_signal(symbol="ETHUSDT", created_offset=-2, status="TOUCHED"),
        raw_signal(symbol="SOLUSDT", created_offset=-3, status="CANCELLED", cancel_offset=-60),
        raw_signal(symbol="XRPUSDT", created_offset=-4, operation_state="CLOSED", result="TP1"),
    ]
    app = create_app(InMemorySignalRepository(docs))
    # Auth is implemented separately; tests inject a trusted active principal rather than
    # weakening production endpoints with a development bypass token.
    principal = Principal("user-42", {"sub": "user-42", "plan": "premium", "plan_end": iso(60).isoformat()}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as test_client:
        yield test_client


def test_health_identifies_non_persistent_dev_storage(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["storage"] == {"status": "ok", "adapter": "memory", "persistent": False}
    assert response.json()["telegram_notification_delivery"]["status"] in {"not_started", "ok"}
    assert "telegram-notification-delivery" in response.json()["workers"]


def test_list_returns_rich_signal_and_ui_aliases(client):
    response = client.get("/signals")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2  # stale cancelled and closed result are hidden like the current mock UI
    btc = data[0]
    assert btc["symbol"] == "BTCUSDT"
    assert btc["entry_low"] == 99.5 and btc["entry_high"] == 100.5
    assert [btc["tp1"], btc["tp2"], btc["tp3"]] == [104, 108, 112]
    # Complete canonical / motor data is retained, not reduced to the 8 displayed fields.
    assert btc["strategy"]["custom_motor_field"] == {"preserve": True}
    assert btc["strategy"]["atr_pct"] == 1.25
    assert btc["scoring"]["normalized_score"] == 86
    assert btc["score_breakdown"]["liquidity"] == 12
    assert btc["score_breakdown"]["legacy_model"] == 8
    assert btc["leverage"] == 5
    assert btc["status"] == "PENDING"
    assert btc["event"]["type"] == "new"


def test_symbol_lookup_is_case_insensitive_and_returns_latest(client):
    response = client.get("/signals/btcusdt")
    assert response.status_code == 200
    assert response.json()["symbol"] == "BTCUSDT"
    assert response.json()["id"].startswith("legacy-BTCUSDT")


def test_missing_symbol_returns_contract_error(client):
    response = client.get("/signals/DOESNOTEXIST")
    assert response.status_code == 404
    assert response.json()["code"] == "SIGNAL_NOT_FOUND"


def test_limit_is_bounded_by_ui_contract(client):
    assert client.get("/signals?limit=100").status_code == 422
    assert client.get("/signals?limit=1").json()[0]["symbol"] == "BTCUSDT"


def test_closed_signal_maps_result_and_event_when_requested_by_symbol():
    app = create_app(InMemorySignalRepository([raw_signal(symbol="ETHUSDT", operation_state="CLOSED", result="TP2")]))
    principal = Principal("user-42", {"sub": "user-42"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as tc:
        response = tc.get("/signals/ETHUSDT")
    assert response.status_code == 200
    data = response.json()
    assert data["operation_result"] == "TP2"
    assert data["event"]["type"] == "closed"
    assert data["event"]["result"] == "TP2"


def test_cancelled_signal_is_visible_only_inside_retention_window():
    fresh = raw_signal(symbol="DOGEUSDT", status="CANCELLED", cancel_offset=-1)
    stale = raw_signal(symbol="BNBUSDT", status="CANCELLED", cancel_offset=-120)
    app = create_app(InMemorySignalRepository([fresh, stale]))
    principal = Principal("user-42", {"sub": "user-42"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as tc:
        symbols = [item["symbol"] for item in tc.get("/signals").json()]
    assert symbols == ["DOGEUSDT"]


def test_missing_bearer_token_is_rejected_when_dependencies_not_overridden(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "test-secret")
    with TestClient(create_app(InMemorySignalRepository([]))) as tc:
        response = tc.get("/signals")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"


def _token(claims: dict, secret: str = "test-secret") -> str:
    def enc(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()
    head = enc(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = enc(json.dumps(claims, separators=(",", ":")).encode())
    body = f"{head}.{payload}"
    sig = enc(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def test_signed_active_token_is_accepted(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "test-secret")
    token = _token({"sub": "u1", "iat": time.time(), "exp": time.time() + 120, "plan": "premium", "plan_end": iso(60).isoformat()})
    with TestClient(create_app(InMemorySignalRepository([raw_signal()]))) as tc:
        response = tc.get("/signals", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_timed_ban_expires_but_permanent_ban_blocks(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "test-secret")
    timed = _token({"sub": "u1", "iat": time.time(), "exp": time.time() + 120, "plan": "premium", "plan_end": iso(60).isoformat(), "banned": True, "ban_until": iso(-10).isoformat()})
    permanent = _token({"sub": "u1", "iat": time.time(), "exp": time.time() + 120, "plan": "premium", "plan_end": iso(60).isoformat(), "banned": True})
    with TestClient(create_app(InMemorySignalRepository([raw_signal()]))) as tc:
        expired_ban = tc.get("/signals", headers={"Authorization": f"Bearer {timed}"})
        permanent_ban = tc.get("/signals", headers={"Authorization": f"Bearer {permanent}"})
    assert expired_ban.status_code == 200
    assert permanent_ban.status_code == 403
    assert permanent_ban.json()["code"] == "BANNED"


def test_invalid_query_has_common_error_envelope(client):
    response = client.get("/signals?limit=101")
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert isinstance(response.json()["details"], list)


def test_expired_plan_and_trial_are_denied(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "test-secret")
    token = _token({
        "sub": "expired-user", "iat": time.time(), "exp": time.time() + 120,
        "plan": "premium", "plan_end": iso(-60).isoformat(), "trial_end": iso(-5).isoformat(),
    })
    with TestClient(create_app(InMemorySignalRepository([raw_signal()]))) as tc:
        response = tc.get("/signals", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["code"] == "ACCESS_EXPIRED"


def test_admin_is_not_blocked_by_ban_or_expired_plan(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "test-secret")
    token = _token({
        "sub": "admin-1", "iat": time.time(), "exp": time.time() + 120,
        "is_admin": True, "banned": True,
    })
    with TestClient(create_app(InMemorySignalRepository([raw_signal()]))) as tc:
        response = tc.get("/signals", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_invalid_stored_signal_fails_visibly_instead_of_being_silently_dropped():
    bad = raw_signal(symbol="BTCUSDT")
    bad["entry_price"] = None
    app = create_app(InMemorySignalRepository([bad]))
    principal = Principal("user-42", {"sub": "user-42"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as tc:
        response = tc.get("/signals")
    assert response.status_code == 503
    assert response.json()["code"] == "SIGNAL_DATA_INVALID"
