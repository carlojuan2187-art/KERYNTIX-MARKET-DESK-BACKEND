from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.business import InMemoryBusinessRepository
from api.repositories.signals import InMemorySignalRepository
from api.security import Principal, current_principal, require_analytical_access
from api.services.auth import issue_access_token
from api.services.payments import is_valid_address
from main import create_app

UTC = timezone.utc
WALLET = "0x" + "a" * 40
SENDER = "0x" + "b" * 40


class FakeRest:
    def __init__(self):
        self.rows = [
            {"symbol": "BTCUSDT", "lastPrice": "70000", "openPrice": "69000", "priceChangePercent": "1.44", "highPrice": "71000", "lowPrice": "68000", "quoteVolume": "2000000000"},
            {"symbol": "ETHUSDT", "lastPrice": "3000", "openPrice": "2990", "priceChangePercent": "0.33", "highPrice": "3050", "lowPrice": "2950", "quoteVolume": "900000000"},
            {"symbol": "1000PEPEUSDT", "lastPrice": "0.01", "openPrice": "0.009", "priceChangePercent": "11.11", "highPrice": "0.011", "lowPrice": "0.008", "quoteVolume": "300000000"},
        ]

    async def fetch_24h_tickers(self):
        return self.rows

    async def fetch_funding(self):
        return [{"symbol": row["symbol"], "lastFundingRate": "0.0001"} for row in self.rows]

    async def fetch_klines(self, *args, **kwargs):
        return []

    async def fetch_endpoint_json(self, *args, **kwargs):
        return []

    async def close(self):
        return None


class FakeTelegram:
    def bot_username(self):
        return "keryntix_market_desk_auth_test"

    def is_configured(self):
        return True

    def send_message(self, *args, **kwargs):
        return None


class FakeChain:
    def is_configured(self):
        return True

    def find_matching_transfer(self, sender, payment):
        return None


def put_user(repo: InMemoryAuthRepository, user_id: str = "user-19"):
    now = datetime.now(UTC)
    user = {
        "user_id": user_id, "phone": "+17875551234", "telegram_chat_id": "chat-19",
        "plan": "free", "plan_end": None, "trial_end": now + timedelta(days=4),
        "ref_code": f"ref_{user_id}", "referred_by": None, "banned": False,
        "tz": "America/Puerto_Rico", "watchlist": [], "alert_prefs": {},
        "created_at": now, "last_activity": now,
    }
    repo.create_user_if_absent(user)
    sid = f"session-{user_id}"
    repo.create_session({"id": sid, "user_id": user_id, "refresh_hash": f"hash-{user_id}",
                         "created_at": now, "expires_at": now + timedelta(days=1), "last_activity": now})
    token = issue_access_token(repo.get_user_by_id(user_id), sid, now=now)
    return user, sid, token


def build_app(monkeypatch):
    monkeypatch.setenv("FTRADE_ENV", "development")
    monkeypatch.setenv("PAYMENT_WALLET_ADDRESS", WALLET)
    monkeypatch.setenv("PREMIUM_PRICE_USDT", "40.000")
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "phase19-contract-tests-secret-long-enough-123")
    monkeypatch.setenv("AUTH_CODE_PEPPER", "phase19-contract-tests-pepper-long-enough-123")
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    auth = InMemoryAuthRepository()
    business = InMemoryBusinessRepository()
    cache = InMemoryJsonCache()
    user, sid, token = put_user(auth)
    app = create_app(InMemorySignalRepository([]), market_cache=cache, market_rest_client=FakeRest(),
                     auth_repository=auth, telegram_gateway=FakeTelegram(), business_repository=business,
                     chain_verifier=FakeChain())
    principal = Principal(user["user_id"], {"sub": user["user_id"], "sid": sid, "plan": "free", "trial_end": user["trial_end"], "tz": user["tz"]})
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    return app, cache, auth, business, user, token


def test_symbol_catalog_search_and_preserve_1000_prefix(monkeypatch):
    app, _, _, _, _, _ = build_app(monkeypatch)
    with TestClient(app) as client:
        found = client.get("/symbols?query=pepe")
        single = client.get("/symbols/1000PEPE")
        missing = client.get("/symbols/NOTAREALCOIN")
    assert found.status_code == 200, found.text
    assert [row["symbol"] for row in found.json()] == ["1000PEPEUSDT"]
    assert single.status_code == 200, single.text
    assert single.json()["symbol"] == "1000PEPEUSDT"
    assert missing.status_code == 404
    assert missing.json()["code"] == "SYMBOL_NOT_FOUND"


def test_contract_payment_aliases_use_the_existing_payment_service(monkeypatch):
    app, cache, _, _, _, _ = build_app(monkeypatch)
    with TestClient(app) as client:
        started = client.post("/payments/start")
        assert started.status_code == 200, started.text
        body = started.json()
        assert body["wallet_dest"] == WALLET
        assert body["record"]["status"] == "pending"
        assert body["record"]["id"]
        assert body["expires_at"]
        history = client.get("/payments/history")
        assert history.status_code == 200 and isinstance(history.json(), list)
        verified = client.post("/payments/verify", json={"record_id": body["record"]["id"], "wallet_from": SENDER})
        assert verified.status_code == 200, verified.text
        assert verified.json()["result"] == "not_found"
        assert verified.json()["record"]["status"] == "pending"
    payment_events = [payload for channel, payload in cache.published if channel == "billing:events:v1"]
    assert len(payment_events) >= 2
    assert all(row["event"] == "payment.updated" for row in payment_events)
    assert payment_events[-1]["payload"]["record"]["id"] == body["record"]["id"]


def test_websocket_ticket_is_short_lived_and_single_use(monkeypatch):
    import time

    app, _cache, _auth, _, user, _ = build_app(monkeypatch)
    with TestClient(app) as client:
        ticket_response = client.post("/events/ticket")
        assert ticket_response.status_code == 200, ticket_response.text
        ticket = ticket_response.json()["ticket"]
        assert ticket_response.json()["expires_in"] == 60
        assert ticket_response.json()["websocket_path"] == "/ws/events"
        with client.websocket_connect(f"/ws/events?ticket={ticket}") as connected:
            # Close the client side and let the ASGI route finish its disconnect
            # cleanup before TestClient tears down its worker portal.
            connected.close()
            hub = getattr(app.state, "event_hub", None)
            deadline = time.monotonic() + 2.0
            while hub is not None and hub.listener_count and time.monotonic() < deadline:
                time.sleep(0.01)
            assert hub is not None
            assert hub.listener_count == 0

        with client.websocket_connect(f"/ws/events?ticket={ticket}") as reused:
            with pytest.raises(WebSocketDisconnect):
                reused.receive_json()

def test_compatibility_routes_are_registered_and_admin_aliases_are_protected(monkeypatch):
    from scripts.release_preflight import route_inventory

    app, _, _, _, _, _ = build_app(monkeypatch)
    registered, websocket_routes = route_inventory(app)
    expected = {
        ("GET", "/symbols"), ("GET", "/symbols/{symbol}"),
        ("POST", "/events/ticket"), ("POST", "/payments/start"),
        ("POST", "/payments/verify"), ("GET", "/payments/history"),
        ("GET", "/referrals"), ("GET", "/market/liquidations"),
        ("GET", "/market/movers"), ("GET", "/admin/stats"),
        ("POST", "/admin/users/{user_id}/plan"),
        ("PATCH", "/admin/users/{user_id}/ban"),
        ("PATCH", "/admin/users/{user_id}/unban"),
    }
    assert expected <= registered
    assert "/ws/events" in websocket_routes

def _admin_principal(app, user):
    from api.security import Principal
    app.dependency_overrides[current_principal] = lambda: Principal(
        user["user_id"],
        {"sub": user["user_id"], "sid": f"session-{user['user_id']}", "plan": "free", "trial_end": user["trial_end"]},
        is_admin=True,
    )


def test_admin_trial_filter_uses_true_active_entitlements(monkeypatch):
    app, _, auth, _, user, _ = build_app(monkeypatch)
    now = datetime.now(UTC)
    auth.create_user_if_absent({
        "user_id": "premium-with-trial", "phone": "+17875551235", "telegram_chat_id": "chat-premium",
        "plan": "premium", "plan_end": now + timedelta(days=30), "trial_end": now + timedelta(days=15),
        "ref_code": "ref-premium", "banned": False, "created_at": now,
    })
    auth.create_user_if_absent({
        "user_id": "banned-trial", "phone": "+17875551236", "telegram_chat_id": "chat-banned",
        "plan": "free", "plan_end": None, "trial_end": now + timedelta(days=15),
        "ref_code": "ref-banned", "banned": True, "ban_until": now + timedelta(days=5), "created_at": now,
    })
    _admin_principal(app, user)
    with TestClient(app) as client:
        response = client.get("/admin/users?filter=trial&limit=100")
    assert response.status_code == 200, response.text
    body = response.json()
    ids = [item["user_id"] for item in body["users"]]
    assert user["user_id"] in ids
    assert "premium-with-trial" not in ids
    assert "banned-trial" not in ids
    assert body["total"] == 1


def test_admin_plan_alias_supports_long_duration_idempotently(monkeypatch):
    app, cache, _auth, _, user, _ = build_app(monkeypatch)
    _admin_principal(app, user)
    headers = {"Idempotency-Key": "phase19-long-plan-730d"}
    with TestClient(app) as client:
        first = client.post(f"/admin/users/{user['user_id']}/plan", json={"days": 730, "reason": "Extensión administrativa aprobada"}, headers=headers)
        assert first.status_code == 200, first.text
        second = client.post(f"/admin/users/{user['user_id']}/plan", json={"days": 730, "reason": "Extensión administrativa aprobada"}, headers=headers)
    assert second.status_code == 200, second.text
    first_body, second_body = first.json(), second.json()
    assert first_body["duration_days_requested"] == 730
    assert len(first_body["event_ids"]) == 2
    assert first_body["activation_applied"] is True
    assert second_body["activation_applied"] is False
    assert first_body["plan_end"] == second_body["plan_end"]
    plan_events = [payload for channel, payload in cache.published if channel == "account:events:v1" and payload.get("event") == "plan.changed"]
    assert len(plan_events) == 2
    assert plan_events[-1]["user_id"] == user["user_id"]
    assert plan_events[-1]["payload"]["user"]["plan"] == "premium"


def test_admin_contract_ban_uses_exact_until_timestamp(monkeypatch):
    app, cache, _auth, _, user, _ = build_app(monkeypatch)
    app.dependency_overrides[current_principal] = lambda: Principal(
        "admin-actor", {"sub": "admin-actor", "sid": "session-admin-actor"}, is_admin=True
    )
    until = datetime.now(UTC) + timedelta(hours=25, minutes=17)
    with TestClient(app) as client:
        response = client.patch(f"/admin/users/{user['user_id']}/ban", json={
            "until": until.isoformat(), "reason": "Revisión de seguridad pendiente"
        })
    assert response.status_code == 200, response.text
    returned = datetime.fromisoformat(response.json()["ban_until"].replace("Z", "+00:00"))
    assert abs((returned - until).total_seconds()) < 2
    ban_events = [payload for channel, payload in cache.published if channel == "account:events:v1" and payload.get("event") == "ban.changed"]
    assert ban_events and ban_events[-1]["payload"]["banned"] is True


def test_admin_user_list_contract_shape_is_consistent_for_all_filters(monkeypatch):
    app, _, _auth, _, user, _ = build_app(monkeypatch)
    _admin_principal(app, user)
    with TestClient(app) as client:
        response = client.get("/admin/users?filter=all&page=0&limit=6")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(("users", "total", "page", "limit")).issubset(body)
    assert isinstance(body["users"], list)
    assert body["page"] == 0
    assert body["limit"] == 6
    assert "items" not in body



def test_movers_snapshot_is_published_only_when_snapshot_changes(monkeypatch):
    import asyncio
    from api.repositories.user_features import InMemoryUserFeatureRepository
    from worker.user_alerts import UserAlertProcessor

    class FakeMarketIntelligence:
        async def liquidations(self, window):
            return {"window": window, "total": 0}

        async def movers(self, window):
            return {"window": window, "status": "ok", "updated_epoch": float(window),
                    "updated_at": "2026-10-25T12:00:00Z", "gainers": [], "losers": [], "flat_volume": []}

    cache = InMemoryJsonCache()
    processor = UserAlertProcessor(InMemoryAuthRepository(), InMemoryUserFeatureRepository(), cache,
                                   market_intelligence=FakeMarketIntelligence())
    asyncio.run(processor._refresh_global_alert_snapshots())
    asyncio.run(processor._refresh_global_alert_snapshots())
    events = [payload for channel, payload in cache.published if channel == "market:movers:events:v1"]
    assert [item["payload"]["window"] for item in events] == [5, 15]
    assert all(item["event"] == "movers.snapshot" for item in events)



def test_admin_plan_alias_requires_idempotency_key(monkeypatch):
    app, _, _auth, _, user, _ = build_app(monkeypatch)
    _admin_principal(app, user)
    with TestClient(app) as client:
        response = client.post(f"/admin/users/{user['user_id']}/plan", json={"days": 30, "reason": "Extensión administrativa aprobada"})
    assert response.status_code == 422
