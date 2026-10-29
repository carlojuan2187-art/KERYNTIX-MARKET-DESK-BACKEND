from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.signals import InMemorySignalRepository
from api.services.auth import as_utc, hash_token, handle_telegram_contact, utcnow
from main import create_app


class FakeTelegram:
    def __init__(self, username="ftrade_auth_test"):
        self.username = username
        self.sent = []

    def bot_username(self):
        return self.username

    def send_message(self, chat_id, text, *, reply_markup=None):
        self.sent.append((str(chat_id), text, reply_markup))


class FakeRest:
    async def close(self):
        pass


def configured(monkeypatch, repo=None, telegram=None):
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "test-secret-for-ftrade-phase-five")
    monkeypatch.setenv("AUTH_CODE_PEPPER", "test-code-pepper-for-phase-five")
    monkeypatch.setenv("ACCESS_TOKEN_MAX_TTL_SECONDS", "900")
    monkeypatch.setenv("TRIAL_DAYS", "5")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    repo = repo or InMemoryAuthRepository()
    telegram = telegram or FakeTelegram()
    app = create_app(
        InMemorySignalRepository([]),
        market_cache=InMemoryJsonCache(),
        market_rest_client=FakeRest(),
        auth_repository=repo,
        telegram_gateway=telegram,
    )
    return app, repo, telegram


def challenge_and_link(client, repo, phone="+17875551234", telegram_user_id="4242", chat_id="4242"):
    response = client.post("/auth/phone", json={"phone": phone})
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    request_doc = repo.get_auth_request(hash_token(token))
    linked = handle_telegram_contact(
        repo,
        token_hash=str(request_doc["token_hash"]),
        sender_user_id=telegram_user_id,
        contact_user_id=telegram_user_id,
        chat_id=chat_id,
        contact_phone=phone,
        username="ftrade_tester",
    )
    return token, linked["code"]


def test_phone_telegram_code_creates_free_trial_user_and_httponly_refresh(monkeypatch):
    app, repo, _telegram = configured(monkeypatch)
    with TestClient(app) as client:
        token, code = challenge_and_link(client, repo)
        check = client.get("/auth/telegram-check", params={"token": token})
        assert check.status_code == 200
        assert check.json() == {"linked": True, "telegram_chat_id": None}

        response = client.post("/auth/verify", json={"phone": "+17875551234", "code": code})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["access_token"]
        assert payload["is_new"] is True
        assert payload["user"]["phone"] == "+17875551234"
        assert payload["user"]["plan"] == "free"
        assert payload["user"]["telegram_chat_id"] == "4242"
        assert payload["user"]["is_admin"] is False
        assert payload["user"]["alert_prefs"] == {"on": True, "liq": True, "mov": True, "vol": True}
        assert "ftrade_refresh" not in payload
        cookie = response.cookies.get("ftrade_refresh")
        assert cookie
        # Fallback copy for browsers that block the cross-site cookie.
        assert payload["refresh_token"] == cookie
        set_cookie = response.headers["set-cookie"].lower()
        assert "httponly" in set_cookie and "samesite=lax" in set_cookie

        headers = {"Authorization": f"Bearer {payload['access_token']}"}
        me = client.get("/auth/me", headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["user_id"] == payload["user"]["user_id"]
        sessions = client.get("/sessions", headers=headers)
        assert sessions.status_code == 200
        assert len(sessions.json()) == 1 and sessions.json()[0]["current"] is True


def test_verify_existing_user_with_symbol_watchlist_returns_success(monkeypatch):
    app, repo, _telegram = configured(monkeypatch)
    with TestClient(app) as client:
        _token, code = challenge_and_link(client, repo)
        first_login = client.post("/auth/verify", json={"phone": "+17875551234", "code": code})
        assert first_login.status_code == 200, first_login.text
        user_id = first_login.json()["user"]["user_id"]

        # Persist the same shape used by the repository in production: symbol strings.
        repo.update_user(user_id, {"watchlist": ["BTCUSDT"]})

        # Expire the prior request's cooldown to model a later login attempt.
        from datetime import datetime, timezone
        previous = repo.get_latest_login_request_for_phone("+17875551234")
        repo.update_auth_request(
            str(previous["token_hash"]),
            {"created_at": datetime.now(timezone.utc) - timedelta(seconds=61)},
        )

        # Existing-account login must serialize without a response-validation error.
        _token, code = challenge_and_link(client, repo)
        login = client.post("/auth/verify", json={"phone": "+17875551234", "code": code})
        assert login.status_code == 200, login.text
        assert login.json()["is_new"] is False
        assert login.json()["user"]["watchlist"] == ["BTCUSDT"]


def test_production_refresh_cookie_allows_cross_origin_frontend(monkeypatch):
    app, repo, _telegram = configured(monkeypatch)
    monkeypatch.setenv("FTRADE_ENV", "production")
    with TestClient(app) as client:
        _token, code = challenge_and_link(client, repo)
        response = client.post("/auth/verify", json={"phone": "+17875551234", "code": code})
        assert response.status_code == 200, response.text
        cookie = response.headers["set-cookie"].lower()
        assert "samesite=none" in cookie
        assert "secure" in cookie
        assert "httponly" in cookie


def test_patch_me_allows_only_preferences_and_rejects_bad_timezone(monkeypatch):
    app, repo, _telegram = configured(monkeypatch)
    with TestClient(app) as client:
        _token, code = challenge_and_link(client, repo)
        login = client.post("/auth/verify", json={"phone": "+17875551234", "code": code})
        access = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {access}"}
        bad = client.patch("/auth/me", json={"tz": "Not/AZone"}, headers=headers)
        assert bad.status_code == 422
        good = client.patch("/auth/me", json={"tz": "America/Puerto_Rico", "exchange": "Binance", "risk_profile": {"capital": 500, "risk_pct": 1.5}}, headers=headers)
        assert good.status_code == 200, good.text
        body = good.json()
        assert body["tz"] == "America/Puerto_Rico"
        assert body["exchange"] == "Binance"
        assert body["risk_profile"] == {"capital": 500, "risk_pct": 1.5}
        forbidden = client.patch("/auth/me", json={"plan": "premium"}, headers=headers)
        assert forbidden.status_code == 422


def test_refresh_rotates_cookie_and_logout_revokes_access_session(monkeypatch):
    monkeypatch.setenv("AUTH_REFRESH_GRACE_SECONDS", "0")
    app, repo, _telegram = configured(monkeypatch)
    with TestClient(app) as client:
        _token, code = challenge_and_link(client, repo)
        login = client.post("/auth/verify", json={"phone": "+17875551234", "code": code})
        old_access = login.json()["access_token"]
        old_refresh = login.cookies.get("ftrade_refresh")
        refreshed = client.post("/auth/refresh")
        assert refreshed.status_code == 200, refreshed.text
        new_access = refreshed.json()["access_token"]
        assert new_access != old_access
        new_refresh = refreshed.cookies.get("ftrade_refresh")
        assert new_refresh and new_refresh != old_refresh
        stale = client.post("/auth/refresh", headers={"Cookie": f"ftrade_refresh={old_refresh}"})
        assert stale.status_code == 401

        logout = client.post("/auth/logout", headers={"Cookie": f"ftrade_refresh={new_refresh}"})
        assert logout.status_code == 204
        check = client.get("/auth/me", headers={"Authorization": f"Bearer {new_access}"})
        assert check.status_code == 401


def test_refresh_works_from_body_token_when_cookie_is_blocked(monkeypatch):
    app, repo, _telegram = configured(monkeypatch)
    with TestClient(app) as client:
        _token, code = challenge_and_link(client, repo)
        login = client.post("/auth/verify", json={"phone": "+17875551234", "code": code})
        stored = login.json()["refresh_token"]
        client.cookies.clear()  # browser dropped the third-party cookie
        refreshed = client.post("/auth/refresh", json={"refresh_token": stored})
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["refresh_token"] and refreshed.json()["refresh_token"] != stored
        client.cookies.clear()
        assert client.post("/auth/refresh").status_code == 401


def test_lost_rotation_response_is_recovered_within_grace_window(monkeypatch):
    monkeypatch.setenv("AUTH_REFRESH_GRACE_SECONDS", "60")
    app, repo, _telegram = configured(monkeypatch)
    with TestClient(app) as client:
        _token, code = challenge_and_link(client, repo)
        login = client.post("/auth/verify", json={"phone": "+17875551234", "code": code})
        first = login.json()["refresh_token"]
        client.cookies.clear()
        assert client.post("/auth/refresh", json={"refresh_token": first}).status_code == 200
        # The client never received that response and retries with the old token.
        client.cookies.clear()
        retry = client.post("/auth/refresh", json={"refresh_token": first})
        assert retry.status_code == 200, retry.text
        # An unknown token is still rejected.
        client.cookies.clear()
        assert client.post("/auth/refresh", json={"refresh_token": "x" * 40}).status_code == 401


def test_bad_contact_is_rejected_and_code_has_five_attempt_limit(monkeypatch):
    app, repo, _telegram = configured(monkeypatch)
    with TestClient(app) as client:
        start = client.post("/auth/phone", json={"phone": "+17875551234"})
        token = start.json()["token"]
        request_doc = repo.get_auth_request(hash_token(token))
        try:
            handle_telegram_contact(
                repo, token_hash=request_doc["token_hash"], sender_user_id="1", contact_user_id="2",
                chat_id="1", contact_phone="+17875551234", username=None,
            )
            assert False, "expected mismatch"
        except ValueError as exc:
            assert str(exc) == "CONTACT_OWNER_MISMATCH"

        linked = handle_telegram_contact(
            repo, token_hash=request_doc["token_hash"], sender_user_id="1", contact_user_id="1",
            chat_id="1", contact_phone="+17875551234", username=None,
        )
        for attempt in range(4):
            wrong = client.post("/auth/verify", json={"phone": "+17875551234", "code": "000000" if linked["code"] != "000000" else "000001"})
            assert wrong.status_code == 422, wrong.text
        last = client.post("/auth/verify", json={"phone": "+17875551234", "code": "000000" if linked["code"] != "000000" else "000001"})
        assert last.status_code == 429


def test_telegram_link_flow_requires_same_registered_phone(monkeypatch):
    app, repo, _telegram = configured(monkeypatch)
    with TestClient(app) as client:
        _token, code = challenge_and_link(client, repo, phone="+17875551234")
        login = client.post("/auth/verify", json={"phone": "+17875551234", "code": code})
        assert login.status_code == 200
        user = login.json()["user"]
        # First remove the current Telegram binding through the actual authenticated route.
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        removed = client.delete("/account/telegram", headers=headers)
        assert removed.status_code == 200
        assert removed.json()["telegram_chat_id"] is None
        link = client.post("/account/telegram", headers=headers)
        assert link.status_code == 200, link.text
        assert link.json()["deep_link"].startswith("https://t.me/ftrade_auth_test?start=link_")
        token = link.json()["token"]
        request_doc = repo.get_auth_request(hash_token(token))
        try:
            handle_telegram_contact(
                repo, token_hash=request_doc["token_hash"], sender_user_id="9", contact_user_id="9",
                chat_id="9", contact_phone="+17875559999", username="someone",
            )
            assert False, "expected registered phone mismatch"
        except ValueError as exc:
            assert str(exc) == "PHONE_MISMATCH"
        linked = handle_telegram_contact(
            repo, token_hash=request_doc["token_hash"], sender_user_id="4242", contact_user_id="4242",
            chat_id="4242", contact_phone="+17875551234", username="ftrade_tester",
        )
        assert linked["flow"] == "link"
        assert linked["code"] is None
        refreshed_user = repo.get_user_by_id(user["user_id"])
        assert refreshed_user["telegram_chat_id"] == "4242"


def test_active_trial_can_read_analytical_api_and_database_ban_applies_immediately(monkeypatch):
    from api.repositories.signals import InMemorySignalRepository

    app, repo, _telegram = configured(monkeypatch)
    with TestClient(app) as client:
        _token, code = challenge_and_link(client, repo)
        login = client.post("/auth/verify", json={"phone": "+17875551234", "code": code})
        access = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {access}"}
        allowed = client.get("/signals", headers=headers)
        assert allowed.status_code == 200, allowed.text

        user_id = login.json()["user"]["user_id"]
        repo.update_user(user_id, {"banned": True, "ban_reason": "manual review"})
        blocked = client.get("/signals", headers=headers)
        assert blocked.status_code == 403
        assert blocked.json()["code"] == "BANNED"
        assert blocked.json()["reason"] == "manual review"


def test_ip_challenge_rate_limit_is_independent_of_phone_numbers(monkeypatch):
    monkeypatch.setenv("PHONE_CHALLENGE_IP_LIMIT", "3")
    app, _repo, _telegram = configured(monkeypatch)
    with TestClient(app) as client:
        for suffix in ("1234", "2345", "3456"):
            result = client.post("/auth/phone", json={"phone": f"+1787555{suffix}"})
            assert result.status_code == 200, result.text
        result = client.post("/auth/phone", json={"phone": "+17875554567"})
        assert result.status_code == 429
        assert result.json()["code"] == "TOO_MANY_REQUESTS"


def test_production_rejects_legacy_tokens_without_persisted_session(monkeypatch):
    import base64
    import hashlib
    import hmac
    import json
    import time

    monkeypatch.setenv("FTRADE_ENV", "production")
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "test-secret-for-ftrade-phase-five")
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    def enc(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode()
    now = time.time()
    header = enc(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = enc(json.dumps({"sub": "not-an-admin", "iat": now, "exp": now + 120, "is_admin": True, "banned": True}).encode())
    body = f"{header}.{claims}"
    signature = enc(hmac.new(b"test-secret-for-ftrade-phase-five", body.encode(), hashlib.sha256).digest())
    token = f"{body}.{signature}"
    app = create_app(
        InMemorySignalRepository([]), market_cache=InMemoryJsonCache(), market_rest_client=FakeRest(),
        auth_repository=InMemoryAuthRepository(), telegram_gateway=FakeTelegram(),
    )
    with TestClient(app) as client:
        response = client.get("/signals", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["code"] == "LEGACY_TOKEN_NOT_ALLOWED"


def test_auth_route_refuses_placeholder_telegram_configuration(monkeypatch):
    from api.services.telegram import TelegramBotAPI

    monkeypatch.setenv("TELEGRAM_AUTH_BOT_USERNAME", "your_new_ftrade_auth_bot")
    gateway = TelegramBotAPI(token="replace-with-a-dedicated-bot-token")
    app = create_app(
        InMemorySignalRepository([]), market_cache=InMemoryJsonCache(), market_rest_client=FakeRest(),
        auth_repository=InMemoryAuthRepository(), telegram_gateway=gateway,
    )
    with TestClient(app) as client:
        response = client.post("/auth/phone", json={"phone": "+17875551234"})
    assert response.status_code == 503
    assert response.json()["code"] == "TELEGRAM_NOT_CONFIGURED"
