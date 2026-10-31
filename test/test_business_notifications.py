from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.business import InMemoryBusinessRepository
from api.repositories.user_features import InMemoryUserFeatureRepository
from api.services.business_notifications import BusinessNotificationService
from api.services.plans import PlanMaintenanceService, PlanService
from api.services.referrals import ReferralService
from worker.telegram_notification_delivery import TelegramNotificationDeliveryWorker
from worker.web_push_delivery import WebPushDeliveryWorker

UTC = timezone.utc


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def is_configured(self):
        return True

    def send_message(self, chat_id, text):
        self.sent.append((str(chat_id), str(text)))


class FakePush:
    def __init__(self):
        self.sent = []

    def is_configured(self):
        return True

    async def send(self, subscription, payload, *, ttl_seconds=300):
        self.sent.append((subscription["id"], dict(payload), ttl_seconds))


def add_user(auth, user_id="u1", *, plan="free", trial_end=None, plan_end=None,
             chat_id="__auto__", banned=False, prefs=None, referred_by=None, now=None, language="en"):
    now = now or datetime.now(UTC)
    doc = {
        "user_id": user_id,
        "phone": f"+1787{abs(hash(user_id)) % 10**7:07d}",
        "telegram_chat_id": f"chat-{user_id}" if chat_id == "__auto__" else chat_id,
        "plan": plan,
        "trial_end": trial_end,
        "plan_end": plan_end,
        "banned": banned,
        "ban_until": None,
        "ref_code": f"ref_{user_id}",
        "referred_by": referred_by,
        "alert_prefs": prefs or {"on": True, "plans": True, "in_app": True, "telegram": True, "web_push": False},
        "risk_profile": {"capital": 1000, "risk_pct": 1},
        "watchlist": [],
        "language": language,
    }
    auth.create_user_if_absent(doc)
    return doc


def test_business_notification_is_durable_and_idempotent_across_channels():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository()
    add_user(auth, now=now)
    features = InMemoryUserFeatureRepository()
    service = BusinessNotificationService(auth, features, InMemoryJsonCache())
    fields = dict(user_id="u1", event_id="payment-completed:pay-1", title="Premium activado",
                  message="Tu pago fue confirmado.", business_event="payment.completed",
                  metadata={"payment_id": "pay-1"}, now=now)

    first = service.enqueue(**fields)
    second = service.enqueue(**fields)

    assert first["created"] is True
    assert second["created"] is False
    assert second["reason"] == "DUPLICATE_EVENT"
    row = features.list_notifications("u1")[0]
    assert row["metadata"]["transactional"] is True
    assert row["metadata"]["business_event"] == "payment.completed"
    assert row["in_app"] is True
    assert row["telegram_delivery"]["status"] == "pending"
    assert len(features.list_notifications("u1")) == 1


def test_business_notification_respects_plan_notification_preference():
    auth = InMemoryAuthRepository()
    add_user(auth, prefs={"on": True, "plans": False, "in_app": True, "telegram": True, "web_push": True})
    features = InMemoryUserFeatureRepository()
    service = BusinessNotificationService(auth, features)

    result = service.enqueue("u1", event_id="plan:1", title="Premium", message="Actualizado", business_event="plan.changed")

    assert result["created"] is False
    assert result["reason"] == "PREFERENCES_DISABLED"
    assert features.list_notifications("u1") == []


def test_transactional_plan_notice_reaches_expired_account_via_telegram():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository()
    add_user(auth, trial_end=now - timedelta(days=2), chat_id="chat-expired", now=now,
             prefs={"on": True, "plans": True, "in_app": True, "telegram": True, "web_push": False})
    features = InMemoryUserFeatureRepository()
    notifications = BusinessNotificationService(auth, features)
    notifications.enqueue("u1", event_id="payment:confirmed", title="Pago confirmado", message="Premium está activo.",
                          business_event="payment.completed", now=now)
    telegram = FakeTelegram()
    worker = TelegramNotificationDeliveryWorker(features, auth, telegram, InMemoryJsonCache(), clock=lambda: now)

    status = asyncio.run(worker.run_once(now=now))

    assert status["sent"] == 1
    assert len(telegram.sent) == 1
    assert features.list_notifications("u1")[0]["telegram_delivery"]["status"] == "sent"


def test_ban_still_suppresses_transactional_telegram_notice():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository()
    add_user(auth, trial_end=now + timedelta(days=3), chat_id="chat-banned", banned=True, now=now)
    features = InMemoryUserFeatureRepository()
    BusinessNotificationService(auth, features).enqueue("u1", event_id="plan:ban-test", title="Plan", message="Estado", business_event="plan.changed", now=now)
    telegram = FakeTelegram()
    worker = TelegramNotificationDeliveryWorker(features, auth, telegram, InMemoryJsonCache(), clock=lambda: now)

    status = asyncio.run(worker.run_once(now=now))

    assert status["suppressed"] == 1
    assert telegram.sent == []
    assert features.list_notifications("u1")[0]["telegram_delivery"]["status"] == "suppressed"


def test_transactional_plan_notice_can_queue_in_app_without_telegram_link():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    auth = InMemoryAuthRepository()
    add_user(auth, trial_end=now + timedelta(hours=2), chat_id=None, now=now,
             prefs={"on": True, "plans": True, "in_app": True, "telegram": True, "web_push": False})
    features = InMemoryUserFeatureRepository()
    service = BusinessNotificationService(auth, features)
    maintenance = PlanMaintenanceService(auth, FakeTelegram(), business_notifications=service, warning_hours=24)

    result = maintenance.run_once(now=now)

    assert result["queued"] == 1
    assert result["skipped_unlinked"] == 0
    rows = features.list_notifications("u1")
    assert len(rows) == 1
    assert rows[0]["metadata"]["business_event"] == "plan.trial_ending"
    assert rows[0]["in_app"] is True
    assert "telegram_delivery" not in rows[0]


def test_referral_activation_and_reward_enqueue_deduplicated_notifications():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository()
    add_user(auth, "referrer", now=now, trial_end=now + timedelta(days=1))
    business = InMemoryBusinessRepository()
    features = InMemoryUserFeatureRepository()
    notifications = BusinessNotificationService(auth, features)
    referrals = ReferralService(auth, business, PlanService(auth), business_notifications=notifications)
    for idx in range(5):
        uid = f"referred-{idx}"
        add_user(auth, uid, plan="premium", plan_end=now + timedelta(days=30), trial_end=None,
                 referred_by="referrer", now=now)
        referrals.on_premium_activated(uid, event_id=f"payment:{uid}", now=now)

    rows = features.list_notifications("referrer", limit=50)
    event_ids = {row["event_id"] for row in rows}
    rewards = [row for row in rows if row["metadata"].get("business_event") == "referral.reward_applied"]
    assert len([event for event in event_ids if event.startswith("referral-premium:")]) == 5
    assert len(rewards) == 1
    assert rewards[0]["metadata"]["days"] == 30
    # Replaying the fifth referral must not produce a second notification/reward.
    referrals.on_premium_activated("referred-4", event_id="payment:referred-4", now=now)
    assert len(features.list_notifications("referrer", limit=50)) == 6


def test_transactional_plan_notice_reaches_expired_account_via_web_push():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository()
    add_user(auth, trial_end=now - timedelta(days=2), chat_id=None, now=now,
             prefs={"on": True, "plans": True, "in_app": True, "telegram": False, "web_push": True})
    features = InMemoryUserFeatureRepository()
    features.create_or_update_push_subscription({
        "id": "push-expired-1", "user_id": "u1", "endpoint": "https://fcm.googleapis.com/fcm/send/test",
        "endpoint_hash": "endpoint-hash-expired-1", "p256dh": "p" * 40, "auth": "a" * 16,
        "user_agent": "test", "active": True, "created_at": now, "updated_at": now,
    })
    notifications = BusinessNotificationService(auth, features)
    notifications.enqueue("u1", event_id="payment:push-confirmed", title="Pago confirmado",
                          message="Premium está activo.", business_event="payment.completed", now=now)
    gateway = FakePush()
    worker = WebPushDeliveryWorker(features, auth, gateway, InMemoryJsonCache(), clock=lambda: now)

    status = asyncio.run(worker.run_once(now=now))

    assert status["sent"] == 1
    assert len(gateway.sent) == 1
    assert gateway.sent[0][1]["title"] == "Pago confirmado"
    row = features.list_notifications("u1")[0]
    assert row["web_push_delivery"]["status"] == "sent"


def test_payment_service_enqueues_state_changes_with_stable_event_ids(monkeypatch):
    from api.repositories.business import InMemoryBusinessRepository
    from api.services.payments import PaymentService
    from api.services.plans import PlanService

    wallet = "0x" + "a" * 40
    sender = "0x" + "b" * 40
    monkeypatch.setenv("PAYMENT_WALLET_ADDRESS", wallet)
    monkeypatch.setenv("PREMIUM_PRICE_USDT", "40.00")
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository()
    add_user(auth, "payer", now=now, trial_end=now + timedelta(days=2))
    features = InMemoryUserFeatureRepository()
    notifications = BusinessNotificationService(auth, features)
    business = InMemoryBusinessRepository()

    class Chain:
        def is_configured(self):
            return True

        def find_matching_transfer(self, _sender, payment):
            return {"txid": "0x" + "c" * 64 + ":1", "tx_hash": "0x" + "c" * 64,
                    "log_index": 1, "block_number": 123, "timestamp": int(now.timestamp()),
                    "value_units": int(payment["expected_units"]) * 10**12}

    service = PaymentService(business, PlanService(auth), Chain(), business_notifications=notifications)
    payment = service.create_or_get("payer", now=now)
    created_ids = {row["event_id"] for row in features.list_notifications("payer")}
    assert f"payment-created:{payment['payment_id']}" in created_ids

    verified = service.verify("payer", payment["payment_id"], sender, now=now + timedelta(seconds=1))
    assert verified["payment"]["status"] == "completed"
    event_ids = {row["event_id"] for row in features.list_notifications("payer", limit=20)}
    assert f"payment-created:{payment['payment_id']}" in event_ids
    assert f"payment-completed:{payment['payment_id']}" in event_ids
    # Replaying a completed state must not create another inbox/outbox record.
    service.verify("payer", payment["payment_id"], sender, now=now + timedelta(seconds=2))
    assert len(features.list_notifications("payer", limit=20)) == len(event_ids)


def test_async_business_notification_publishes_in_app_hint_after_durable_enqueue():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository()
    add_user(auth, now=now)
    features = InMemoryUserFeatureRepository()
    cache = InMemoryJsonCache()
    service = BusinessNotificationService(auth, features, cache)

    result = asyncio.run(service.notify("u1", event_id="plan:realtime-test", title="Premium actualizado",
                                        message="Tu plan cambió.", business_event="plan.changed", now=now))

    assert result["created"] is True
    assert result["notification_id"]
    assert len(cache.published) == 1
    channel, event = cache.published[0]
    assert channel == "notifications:events:v1"
    assert event["event"] == "notification.created"
    assert event["user_id"] == "u1"


def test_business_notifications_are_localized_before_persisting_and_queuing_telegram():
    auth = InMemoryAuthRepository()
    add_user(auth, "english-user", language="en", chat_id="chat-en")
    add_user(auth, "spanish-user", language="es", chat_id="chat-es")
    features = InMemoryUserFeatureRepository()
    service = BusinessNotificationService(auth, features)

    english = service.enqueue("english-user", event_id="referral:en", title="Un referido activó Premium",
                              message="Una persona invitada mediante tu enlace ha activado Premium.",
                              business_event="referral.premium_activated")
    spanish = service.enqueue("spanish-user", event_id="referral:es", title="Un referido activó Premium",
                              message="Una persona invitada mediante tu enlace ha activado Premium.",
                              business_event="referral.premium_activated")

    english_row = features.list_notifications("english-user")[0]
    spanish_row = features.list_notifications("spanish-user")[0]
    assert english["created"] and spanish["created"]
    assert english_row["title"] == "A referral activated Premium"
    assert english_row["telegram_delivery"]["text"].startswith("A referral activated Premium")
    assert spanish_row["title"] == "Un referido activó Premium"
    assert spanish_row["telegram_delivery"]["text"].startswith("Un referido activó Premium")
