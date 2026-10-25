from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.signals import InMemorySignalRepository
from api.services.auth import issue_access_token
from main import create_app

UTC = timezone.utc


class FakeTelegram:
    def bot_username(self):
        return "ftrade_auth_test"

    def is_configured(self):
        return True

    def send_message(self, chat_id, text, *, reply_markup=None):
        return None


class FakeRest:
    async def close(self):
        return None


def setup_api(monkeypatch, *, trial_end, is_admin=False):
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "test-secret-for-ftrade-plans-phase-ten")
    monkeypatch.setenv("AUTH_CODE_PEPPER", "test-code-pepper-for-plans-phase-ten")
    monkeypatch.setenv("FTRADE_ENV", "development")
    user_id = "usr_plan_api_admin" if is_admin else "usr_plan_api_user"
    monkeypatch.setenv("ADMIN_USER_IDS", user_id if is_admin else "")
    now = datetime.now(UTC)
    repo = InMemoryAuthRepository()
    user = {
        "user_id": user_id,
        "phone": "+17875551234",
        "telegram_chat_id": "4242",
        "plan": "free",
        "trial_end": trial_end,
        "plan_end": None,
        "ref_code": "ref_plan_api",
        "risk_profile": {"capital": 1000.0, "risk_pct": 1.0},
        "alert_prefs": {"on": True, "liq": True, "mov": True, "vol": True},
        "tz": "America/Puerto_Rico",
        "banned": False,
        "created_at": now,
        "last_activity": now,
    }
    repo.create_user_if_absent(user)
    session_id = "session-plan-api"
    repo.create_session({
        "id": session_id, "user_id": user_id, "refresh_hash": "unused-test-refresh-hash",
        "created_at": now, "expires_at": now + timedelta(days=1), "last_activity": now,
        "device": "test", "location": "test",
    })
    token = issue_access_token(user, session_id, now=now)
    app = create_app(
        InMemorySignalRepository([]), market_cache=InMemoryJsonCache(),
        market_rest_client=FakeRest(), auth_repository=repo, telegram_gateway=FakeTelegram(),
    )
    return app, repo, token, user_id


def test_account_plan_endpoint_reports_trial_and_entitlements(monkeypatch):
    now = datetime.now(UTC)
    app, _repo, token, _user_id = setup_api(monkeypatch, trial_end=now + timedelta(days=2))
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        plan = client.get("/account/plan", headers=headers)
        entitlements = client.get("/account/entitlements", headers=headers)
        assert plan.status_code == 200, plan.text
        assert plan.json()["state"] == "trial"
        assert plan.json()["allowed"] is True
        assert entitlements.status_code == 200
        assert entitlements.json()["features"]["signals"] is True


def test_expiry_is_enforced_on_next_request_even_with_old_access_token(monkeypatch):
    now = datetime.now(UTC)
    app, repo, token, user_id = setup_api(monkeypatch, trial_end=now + timedelta(days=1))
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        allowed = client.get("/signals", headers=headers)
        assert allowed.status_code == 200
        repo.update_user(user_id, {"trial_end": datetime.now(UTC) - timedelta(seconds=1)})
        plan = client.get("/account/plan", headers=headers)
        denied = client.get("/signals", headers=headers)
        assert plan.status_code == 200 and plan.json()["state"] == "expired"
        assert denied.status_code == 403 and denied.json()["code"] == "ACCESS_EXPIRED"


def test_admin_entitlement_bypasses_expired_trial(monkeypatch):
    now = datetime.now(UTC)
    app, _repo, token, _user_id = setup_api(monkeypatch, trial_end=now - timedelta(days=1), is_admin=True)
    with TestClient(app) as client:
        response = client.get("/signals", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
