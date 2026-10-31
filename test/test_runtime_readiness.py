from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.signals import InMemorySignalRepository
from main import PRODUCTION_REQUIRED_WORKERS, create_app


class FakeRest:
    async def close(self):
        return None


class FakeTelegram:
    def send_message(self, chat_id, text, *, reply_markup=None):
        return None

    def bot_username(self):
        return "ftrade_test_bot"

    def is_configured(self):
        return True


def _app(cache: InMemoryJsonCache):
    return create_app(
        InMemorySignalRepository([]),
        market_cache=cache,
        market_rest_client=FakeRest(),
        auth_repository=InMemoryAuthRepository(),
        telegram_gateway=FakeTelegram(),
    )



def _configure_vapid(monkeypatch):
    import base64
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", base64.urlsafe_b64encode(public).decode("ascii").rstrip("="))
    monkeypatch.setenv("VAPID_PRIVATE_KEY_PEM", pem.decode("utf-8"))
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:push-test@example.com")

def _configure_production(monkeypatch):
    monkeypatch.setenv("FTRADE_ENV", "production")
    monkeypatch.setenv("FTRADE_DEPLOYMENT_NAME", "prod-readiness-test")
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "a" * 48)
    monkeypatch.setenv("AUTH_CODE_PEPPER", "b" * 48)
    monkeypatch.setenv("ADMIN_USER_IDS", "usr_admin_test")
    monkeypatch.setenv("TELEGRAM_AUTH_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyzABCDE123456")
    monkeypatch.setenv("TELEGRAM_AUTH_BOT_USERNAME", "ftrade_prod_bot")
    monkeypatch.setenv("PAYMENT_WALLET_ADDRESS", "0x1111111111111111111111111111111111111111")
    monkeypatch.setenv("USDT_BEP20_CONTRACT", "0x55d398326f99059fF775485246999027B3197955")
    monkeypatch.setenv("BSC_RPC_URL", "https://bsc-dataseed.binance.org")
    monkeypatch.setenv("PREMIUM_PRICE_USDT", "10.00")
    monkeypatch.setenv("TRIAL_DAYS", "5")
    monkeypatch.setenv("CORS_ORIGINS", "https://ftrade.example.com")


def test_development_readiness_does_not_require_workers_or_production_secrets(monkeypatch):
    monkeypatch.setenv("FTRADE_ENV", "development")
    cache = InMemoryJsonCache()
    app = _app(cache)

    with TestClient(app) as client:
        live = client.get("/live")
        health = client.get("/health")
        ready = client.get("/ready")

    assert live.status_code == 200
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["ready"] is True
    assert ready.json()["configuration"]["status"] == "not_required"


def test_production_readiness_degrades_when_required_workers_are_not_started(monkeypatch):
    _configure_production(monkeypatch)
    # Phase 20 adds VAPID to the production readiness contract.
    _configure_vapid(monkeypatch)
    cache = InMemoryJsonCache()
    app = _app(cache)

    with TestClient(app) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    issues = health.json()["readiness_issues"]
    assert "SHARED_REDIS_REQUIRED" in issues
    assert any(issue.startswith("WORKER_NOT_HEALTHY:market-feed:not_started") for issue in issues)
    assert ready.status_code == 503
    assert ready.json()["ready"] is False


def test_production_worker_status_is_stale_after_heartbeat_window(monkeypatch):
    _configure_production(monkeypatch)
    _configure_vapid(monkeypatch)
    cache = InMemoryJsonCache()
    namespace = "prod-readiness-test"
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    for name in PRODUCTION_REQUIRED_WORKERS:
        cache._values[f"ftrade:worker-status:{namespace}:{name}:v1"] = ({
            "worker": name,
            "status": "running",
            "updated_at": old,
        }, None)
    app = _app(cache)

    with TestClient(app) as client:
        health = client.get("/health")

    body = health.json()
    assert body["configuration"]["issues"] == []
    assert body["workers"]["market-feed"]["status"] == "stale"
    assert body["workers"]["market-feed"]["last_status"] == "running"
    assert any(issue.startswith("WORKER_NOT_HEALTHY:market-feed:stale") for issue in body["readiness_issues"])


def test_production_configuration_issues_are_reported_without_leaking_secrets(monkeypatch):
    monkeypatch.setenv("FTRADE_ENV", "production")
    # Readiness must prevent a production deploy from silently changing the
    # published business terms for Premium price or five-day trial duration.
    monkeypatch.setenv("PREMIUM_PRICE_USDT", "40.00")
    monkeypatch.setenv("TRIAL_DAYS", "7")
    for key in (
        "ACCESS_TOKEN_SECRET", "AUTH_CODE_PEPPER", "ADMIN_USER_IDS", "ADMIN_PHONE_NUMBERS", "TELEGRAM_AUTH_BOT_TOKEN",
        "TELEGRAM_AUTH_BOT_USERNAME", "PAYMENT_WALLET_ADDRESS", "CORS_ORIGINS", "FTRADE_DEPLOYMENT_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    cache = InMemoryJsonCache()
    app = _app(cache)

    with TestClient(app) as client:
        ready = client.get("/ready")

    assert ready.status_code == 503
    issues = ready.json()["configuration"]["issues"]
    assert "ACCESS_TOKEN_SECRET_MISSING_OR_WEAK" in issues
    assert "AUTH_CODE_PEPPER_MISSING_OR_WEAK" in issues
    assert "ADMIN_USER_IDS_EMPTY" in issues
    assert "TELEGRAM_BOT_NOT_CONFIGURED" in issues
    assert "USDT_BEP20_PAYMENTS_NOT_CONFIGURED" in issues
    assert "CORS_ORIGINS_MISSING_OR_INSECURE" in issues
    assert "PREMIUM_PRICE_MUST_BE_10_USDT" in issues
    assert "TRIAL_DAYS_MUST_BE_5" in issues
    assert "FTRADE_DEPLOYMENT_NAME_NOT_SET_FOR_PRODUCTION" in issues
    assert "a" * 48 not in ready.text
