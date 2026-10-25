from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.business import InMemoryBusinessRepository
from api.repositories.signals import InMemorySignalRepository
from api.repositories.user_features import InMemoryUserFeatureRepository
from api.services.auth import issue_access_token
from main import create_app

UTC = timezone.utc
WALLET = "0x" + "a" * 40
SENDER = "0x" + "b" * 40


class FakeRest:
    async def close(self):
        return None


class FakeTelegram:
    def bot_username(self):
        return "ftrade_phase22_test_bot"

    def is_configured(self):
        return True

    def send_message(self, *args, **kwargs):
        return None


class FakeChain:
    def __init__(self):
        self.transfer = None

    def is_configured(self):
        return True

    def find_matching_transfer(self, sender, payment):
        return self.transfer


def _user_and_token(auth: InMemoryAuthRepository, user_id: str = "usr_release_customer") -> tuple[dict, str]:
    now = datetime.now(UTC)
    user = {
        "user_id": user_id,
        "phone": "+17875550999",
        "telegram_chat_id": "chat-release-customer",
        "plan": "free",
        "plan_end": None,
        "trial_end": now + timedelta(days=3),
        "ref_code": f"ref_{user_id}",
        "referred_by": None,
        "risk_profile": {"capital": 1000, "risk_pct": 1},
        "watchlist": [],
        "alert_prefs": {"on": True, "plans": True, "in_app": True, "telegram": False, "web_push": False},
        "tz": "America/Puerto_Rico",
        "banned": False,
        "created_at": now,
        "last_activity": now,
    }
    auth.create_user_if_absent(user)
    session_id = f"session_{user_id}"
    auth.create_session({
        "id": session_id, "user_id": user_id, "refresh_hash": f"refresh_{user_id}",
        "created_at": now, "expires_at": now + timedelta(days=1), "last_activity": now,
    })
    token = issue_access_token(auth.get_user_by_id(user_id), session_id, now=now)
    return user, token


def test_full_payment_activation_entitlement_and_notification_journey(monkeypatch):
    monkeypatch.setenv("FTRADE_ENV", "development")
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "release-test-secret-long-enough-0123456789abcdef")
    monkeypatch.setenv("AUTH_CODE_PEPPER", "release-test-pepper-long-enough-0123456789abcdef")
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    monkeypatch.setenv("PAYMENT_WALLET_ADDRESS", WALLET)
    monkeypatch.setenv("PREMIUM_PRICE_USDT", "40.000")

    auth = InMemoryAuthRepository()
    business = InMemoryBusinessRepository()
    features = InMemoryUserFeatureRepository()
    cache = InMemoryJsonCache()
    chain = FakeChain()
    user, token = _user_and_token(auth)
    app = create_app(
        InMemorySignalRepository([]), market_cache=cache, market_rest_client=FakeRest(),
        auth_repository=auth, telegram_gateway=FakeTelegram(), business_repository=business,
        chain_verifier=chain, user_feature_repository=features,
    )
    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        initial = client.get("/account/entitlements", headers=headers)
        assert initial.status_code == 200, initial.text
        assert initial.json()["state"] == "trial"
        assert initial.json()["allowed"] is True

        created_response = client.post("/billing/payments", headers=headers)
        assert created_response.status_code == 200, created_response.text
        payment = created_response.json()["payment"]
        assert payment["status"] == "pending"
        pending_notices = features.list_notifications(user["user_id"], limit=20)
        assert [row["event_id"] for row in pending_notices].count(f"payment-created:{payment['payment_id']}") == 1

        tx_hash = "0x" + "c" * 64
        now_epoch = int(datetime.now(UTC).timestamp())
        chain.transfer = {
            "txid": f"{tx_hash}:0", "tx_hash": tx_hash, "log_index": 0,
            "block_number": 100000, "timestamp": now_epoch,
            "value_units": int(payment["amount_units"]) * (10 ** 12),
        }
        verified = client.post(
            f"/billing/payments/{payment['payment_id']}/verify",
            json={"sender_address": SENDER}, headers=headers,
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["result"] == "ok"
        assert verified.json()["payment"]["status"] == "completed"

        after_payment = client.get("/account/entitlements", headers=headers)
        assert after_payment.status_code == 200, after_payment.text
        assert after_payment.json()["state"] == "premium"
        assert after_payment.json()["premium_active"] is True
        premium_end = auth.get_user_by_id(user["user_id"])["plan_end"]

        # Replaying a completed verification cannot extend Premium a second time.
        replay = client.post(
            f"/billing/payments/{payment['payment_id']}/verify",
            json={"sender_address": SENDER}, headers=headers,
        )
        assert replay.status_code == 200, replay.text
        assert auth.get_user_by_id(user["user_id"])["plan_end"] == premium_end

        notices = features.list_notifications(user["user_id"], limit=20)
        event_ids = [row["event_id"] for row in notices]
        assert event_ids.count(f"payment-created:{payment['payment_id']}") == 1
        assert event_ids.count(f"payment-completed:{payment['payment_id']}") == 1
        assert len(business.payment_txs) == 1


def test_ban_revokes_analytical_access_on_the_next_request(monkeypatch):
    monkeypatch.setenv("FTRADE_ENV", "development")
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "release-ban-secret-long-enough-0123456789abcdef")
    monkeypatch.setenv("AUTH_CODE_PEPPER", "release-ban-pepper-long-enough-0123456789abcdef")
    monkeypatch.setenv("ADMIN_USER_IDS", "usr_release_admin")
    auth = InMemoryAuthRepository()
    business = InMemoryBusinessRepository()
    now = datetime.now(UTC)
    customer = {
        "user_id": "usr_release_customer_ban", "phone": "+17875550888", "telegram_chat_id": "chat-customer-ban",
        "plan": "premium", "plan_end": now + timedelta(days=20), "trial_end": now + timedelta(days=1),
        "ref_code": "ref_release_customer_ban", "banned": False,
        "risk_profile": {"capital": 1000, "risk_pct": 1}, "watchlist": [], "alert_prefs": {},
        "tz": "UTC", "created_at": now, "last_activity": now,
    }
    admin = {
        "user_id": "usr_release_admin", "phone": "+17875550777", "telegram_chat_id": "chat-release-admin",
        "plan": "premium", "plan_end": now + timedelta(days=200), "trial_end": now + timedelta(days=1),
        "ref_code": "ref_release_admin", "banned": False,
        "risk_profile": {"capital": 1000, "risk_pct": 1}, "watchlist": [], "alert_prefs": {},
        "tz": "UTC", "created_at": now, "last_activity": now,
    }
    auth.create_user_if_absent(customer)
    auth.create_user_if_absent(admin)
    tokens = {}
    for row in (customer, admin):
        sid = f"session_{row['user_id']}"
        auth.create_session({"id": sid, "user_id": row["user_id"], "refresh_hash": f"r-{sid}",
                             "created_at": now, "expires_at": now + timedelta(days=1), "last_activity": now})
        tokens[row["user_id"]] = issue_access_token(auth.get_user_by_id(row["user_id"]), sid, now=now)
    app = create_app(InMemorySignalRepository([]), market_cache=InMemoryJsonCache(), market_rest_client=FakeRest(),
                     auth_repository=auth, telegram_gateway=FakeTelegram(), business_repository=business,
                     chain_verifier=FakeChain())
    customer_headers = {"Authorization": f"Bearer {tokens[customer['user_id']]}"}
    admin_headers = {"Authorization": f"Bearer {tokens[admin['user_id']]}"}

    with TestClient(app) as client:
        before = client.get("/signals", headers=customer_headers)
        assert before.status_code == 200, before.text
        ban = client.post(
            f"/admin/users/{customer['user_id']}/ban",
            json={"reason": "Prueba de auditoría de acceso"}, headers=admin_headers,
        )
        assert ban.status_code == 200, ban.text
        denied = client.get("/signals", headers=customer_headers)
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "BANNED"
        own_account_still_available = client.get("/account/plan", headers=customer_headers)
        assert own_account_still_available.status_code == 200, own_account_still_available.text
        unban = client.post(
            f"/admin/users/{customer['user_id']}/unban",
            json={"reason": "Fin de prueba"}, headers=admin_headers,
        )
        assert unban.status_code == 200, unban.text
        restored = client.get("/signals", headers=customer_headers)
        assert restored.status_code == 200, restored.text
