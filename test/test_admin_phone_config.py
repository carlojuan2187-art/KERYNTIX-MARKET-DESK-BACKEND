from api.services.auth import is_admin_id, is_admin_user


def test_admin_can_be_configured_by_verified_phone_number(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    monkeypatch.setenv("ADMIN_PHONE_NUMBERS", "+1 (787) 555-0123")

    assert is_admin_user({"user_id": "usr_not_in_env", "phone": "+17875550123"}) is True
    assert is_admin_user({"user_id": "usr_other", "phone": "+17875550124"}) is False


def test_admin_phone_matching_does_not_replace_legacy_user_id_support(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "usr_legacy_admin")
    monkeypatch.setenv("ADMIN_PHONE_NUMBERS", "")

    assert is_admin_id("usr_legacy_admin") is True
    assert is_admin_user({"user_id": "usr_legacy_admin", "phone": "+17875550000"}) is True
    assert is_admin_user({"user_id": "usr_other", "phone": "+17875550000"}) is False


def test_phone_configuration_does_not_grant_admin_without_matching_saved_phone(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    monkeypatch.setenv("ADMIN_PHONE_NUMBERS", "+17875550123")

    assert is_admin_user({"user_id": "usr_other"}) is False
    assert is_admin_user(None) is False


def test_admin_panel_authorizes_account_by_saved_phone(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from fastapi.testclient import TestClient

    from api.cache import InMemoryJsonCache
    from api.repositories.auth import InMemoryAuthRepository
    from api.repositories.signals import InMemorySignalRepository
    from api.services.auth import issue_access_token
    from main import create_app

    now = datetime.now(timezone.utc)
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "test-secret-for-admin-phone-config-000000")
    monkeypatch.setenv("AUTH_CODE_PEPPER", "test-pepper-for-admin-phone-config-000000")
    monkeypatch.setenv("FTRADE_ENV", "development")
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    monkeypatch.setenv("ADMIN_PHONE_NUMBERS", "+1 (787) 555-0123")

    repository = InMemoryAuthRepository()
    user = {
        "user_id": "usr_phone_admin", "phone": "+17875550123", "telegram_chat_id": "4242",
        "plan": "free", "trial_end": now - timedelta(days=2), "plan_end": None,
        "ref_code": "ref_phone_admin", "risk_profile": {"capital": 1000.0, "risk_pct": 1.0},
        "watchlist": [], "alert_prefs": {"on": True, "liq": True, "mov": True, "vol": True},
        "tz": "America/Puerto_Rico", "banned": False,
        "created_at": now, "last_activity": now,
    }
    repository.create_user_if_absent(user)
    session_id = "session-phone-admin"
    repository.create_session({
        "id": session_id, "user_id": user["user_id"], "refresh_hash": "unused-refresh-hash",
        "created_at": now, "expires_at": now + timedelta(days=1), "last_activity": now,
        "device": "test", "location": "test",
    })
    token = issue_access_token(user, session_id, now=now)

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

    app = create_app(InMemorySignalRepository([]), market_cache=InMemoryJsonCache(),
                     market_rest_client=FakeRest(), auth_repository=repository,
                     telegram_gateway=FakeTelegram())
    with TestClient(app) as client:
        response = client.get("/admin/summary", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
