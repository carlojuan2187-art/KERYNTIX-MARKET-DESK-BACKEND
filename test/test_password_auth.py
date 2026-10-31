from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient

from api.services.auth import handle_telegram_contact, hash_token, utcnow
from test.test_auth_api import configured


def _verify_telegram_for_challenge(client: TestClient, repo, response):
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    challenge = repo.get_auth_request(hash_token(token))
    linked = handle_telegram_contact(
        repo,
        token_hash=str(challenge["token_hash"]),
        sender_user_id="4242",
        contact_user_id="4242",
        chat_id="4242",
        contact_phone=str(challenge["phone"]),
        username="ftrade_tester",
    )
    assert linked["code"]
    return token, linked["code"]


def test_registration_uses_telegram_once_then_password_login_is_direct(monkeypatch):
    app, repo, _telegram = configured(monkeypatch)
    phone = "+17875551234"
    password = "MercadoSeguro-2026!"
    with TestClient(app) as client:
        _token, code = _verify_telegram_for_challenge(
            client,
            repo,
            client.post("/auth/register", json={"phone": phone, "password": password}),
        )
        registered = client.post("/auth/verify", json={"phone": phone, "code": code})
        assert registered.status_code == 200, registered.text
        body = registered.json()
        assert body["is_new"] is True
        assert body["user"]["phone"] == phone
        assert "password_hash" not in body["user"]
        stored = repo.get_user_by_phone(phone)
        assert stored and stored["password_hash"].startswith("pbkdf2_sha256$")
        assert password not in str(stored["password_hash"])

        wrong = client.post("/auth/login", json={"phone": phone, "password": "not-the-password"})
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "INVALID_CREDENTIALS"

        login = client.post("/auth/login", json={"phone": phone, "password": password})
        assert login.status_code == 200, login.text
        assert login.json()["is_new"] is False
        assert login.json()["user"]["user_id"] == body["user"]["user_id"]
        assert login.json()["access_token"]
        assert login.cookies.get("ftrade_refresh")
        assert "password_hash" not in login.json()["user"]


def test_existing_legacy_account_can_set_password_once_then_login_without_telegram(monkeypatch):
    app, repo, _telegram = configured(monkeypatch)
    phone = "+17875551234"
    password = "AccesoNuevo-2026!"
    with TestClient(app) as client:
        _token, first_code = _verify_telegram_for_challenge(
            client, repo, client.post("/auth/phone", json={"phone": phone})
        )
        first = client.post("/auth/verify", json={"phone": phone, "code": first_code})
        assert first.status_code == 200, first.text
        assert not repo.get_user_by_phone(phone).get("password_hash")

        legacy_login = client.post("/auth/login", json={"phone": phone, "password": password})
        assert legacy_login.status_code == 409
        assert legacy_login.json()["code"] == "PASSWORD_SETUP_REQUIRED"

        # Recovery is the only Telegram step for an account created before passwords existed.
        existing_challenge = repo.get_latest_login_request_for_phone(phone)
        repo.update_auth_request(
            str(existing_challenge["token_hash"]),
            {"created_at": utcnow() - timedelta(seconds=61)},
        )
        recovery = client.post("/auth/recover", json={"phone": phone})
        _token, recovery_code = _verify_telegram_for_challenge(client, repo, recovery)
        session = client.post("/auth/verify", json={"phone": phone, "code": recovery_code})
        assert session.status_code == 200, session.text
        headers = {"Authorization": f"Bearer {session.json()['access_token']}"}
        configured_password = client.post(
            "/auth/password", json={"password": password}, headers=headers
        )
        assert configured_password.status_code == 200, configured_password.text
        assert "password_hash" not in configured_password.json()

        client.cookies.clear()
        direct = client.post("/auth/login", json={"phone": phone, "password": password})
        assert direct.status_code == 200, direct.text
        assert direct.json()["user"]["user_id"] == first.json()["user"]["user_id"]


def test_registration_rejects_existing_phone_and_password_schema_is_strict(monkeypatch):
    app, repo, _telegram = configured(monkeypatch)
    phone = "+17875551234"
    with TestClient(app) as client:
        _token, code = _verify_telegram_for_challenge(
            client, repo, client.post("/auth/phone", json={"phone": phone})
        )
        assert client.post("/auth/verify", json={"phone": phone, "code": code}).status_code == 200
        duplicate = client.post(
            "/auth/register", json={"phone": phone, "password": "NewSecurePassword-2026"}
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "ACCOUNT_ALREADY_EXISTS"

        short_password = client.post(
            "/auth/register", json={"phone": "+442071838750", "password": "short"}
        )
        assert short_password.status_code == 422


def test_registration_persists_selected_spanish_language(monkeypatch):
    app, repo, _telegram = configured(monkeypatch)
    phone = "+17875554321"
    password = "MercadoSeguro-2026!"
    with TestClient(app) as client:
        _token, code = _verify_telegram_for_challenge(
            client,
            repo,
            client.post("/auth/register", json={"phone": phone, "password": password, "language": "es"}),
        )
        registered = client.post("/auth/verify", json={"phone": phone, "code": code})
        assert registered.status_code == 200, registered.text
        assert registered.json()["user"]["language"] == "es"
        assert repo.get_user_by_phone(phone)["language"] == "es"
