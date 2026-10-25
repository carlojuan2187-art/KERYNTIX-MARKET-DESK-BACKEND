from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.user_features import InMemoryUserFeatureRepository
from worker.telegram_notification_delivery import TelegramNotificationDeliveryWorker
from worker.user_alerts import UserAlertProcessor
from worker.user_alerts import UserAlertProcessor

UTC = timezone.utc


class FakeTelegram:
    def __init__(self, failures=None, *, configured=True):
        self.failures = list(failures or [])
        self.configured = configured
        self.sent = []

    def is_configured(self):
        return self.configured

    def send_message(self, chat_id, text):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((str(chat_id), str(text)))


def add_user(auth, user_id="telegram-user", *, chat_id="chat-100", prefs=None, active=True):
    now = datetime.now(UTC)
    auth.create_user_if_absent({
        "user_id": user_id,
        "phone": "+1787555" + str(abs(hash(user_id)) % 100000).zfill(5),
        "telegram_chat_id": chat_id,
        "plan": "premium" if active else "free",
        "plan_end": now + timedelta(days=30) if active else None,
        "trial_end": None,
        "banned": False,
        "ref_code": f"ref-{user_id}",
        "alert_prefs": prefs or {"on": True, "liq": True, "mov": True, "vol": True, "price": True,
                                  "signals": True, "plans": True, "in_app": True, "telegram": True},
    })


def add_outbox(features, *, user_id="telegram-user", event_id="event-1", chat_id="chat-100", kind="price", now=None):
    now = now or datetime.now(UTC)
    return features.create_notification({
        "id": f"n-{event_id}", "user_id": user_id, "event_id": event_id, "kind": kind,
        "title": "Alerta de prueba", "message": "Mensaje persistido", "created_at": now,
        "read_at": None, "in_app": True,
        "telegram_delivery": {"status": "pending", "chat_id": chat_id,
                              "text": "Alerta de prueba\n\nMensaje persistido", "attempts": 0,
                              "next_attempt_at": now, "created_at": now, "updated_at": now},
    })[0]


def test_notification_outbox_sends_and_marks_sent():
    auth = InMemoryAuthRepository()
    add_user(auth)
    features = InMemoryUserFeatureRepository()
    now = datetime.now(UTC)
    add_outbox(features, now=now)
    telegram = FakeTelegram()
    worker = TelegramNotificationDeliveryWorker(features, auth, telegram, InMemoryJsonCache())

    status = asyncio.run(worker.run_once(now=now))

    assert status["claimed"] == 1
    assert status["sent"] == 1
    assert telegram.sent == [("chat-100", "Alerta de prueba\n\nMensaje persistido")]
    row = features.list_notifications("telegram-user")[0]
    assert row["telegram_delivery"]["status"] == "sent"
    assert row["telegram_delivery"]["attempts"] == 1


def test_transient_failure_is_durable_and_retried_after_backoff():
    class TemporaryFailure(RuntimeError):
        retryable = True
        retry_after_seconds = None

    auth = InMemoryAuthRepository()
    add_user(auth)
    features = InMemoryUserFeatureRepository()
    now = datetime.now(UTC)
    add_outbox(features, now=now)
    telegram = FakeTelegram([TemporaryFailure("temporary")])
    worker = TelegramNotificationDeliveryWorker(features, auth, telegram, clock=lambda: now,
                                               retry_base_seconds=5, retry_max_seconds=60)

    first = asyncio.run(worker.run_once(now=now))
    assert first["retried"] == 1
    row = features.list_notifications("telegram-user")[0]
    assert row["telegram_delivery"]["status"] == "retry"
    assert row["telegram_delivery"]["next_attempt_at"] == now + timedelta(seconds=5)

    early = asyncio.run(worker.run_once(now=now + timedelta(seconds=4)))
    assert early["claimed"] == 0
    later = asyncio.run(worker.run_once(now=now + timedelta(seconds=5)))
    assert later["sent"] == 1
    assert len(telegram.sent) == 1
    assert features.list_notifications("telegram-user")[0]["telegram_delivery"]["status"] == "sent"


def test_permanent_failure_becomes_dead_without_retry():
    class PermanentFailure(RuntimeError):
        retryable = False

    auth = InMemoryAuthRepository()
    add_user(auth)
    features = InMemoryUserFeatureRepository()
    now = datetime.now(UTC)
    add_outbox(features, now=now)
    worker = TelegramNotificationDeliveryWorker(features, auth, FakeTelegram([PermanentFailure("bad chat")]))

    status = asyncio.run(worker.run_once(now=now))

    assert status["dead"] == 1
    assert features.list_notifications("telegram-user")[0]["telegram_delivery"]["status"] == "dead"


def test_expired_delivery_lease_can_be_reclaimed_after_worker_crash():
    auth = InMemoryAuthRepository()
    add_user(auth)
    features = InMemoryUserFeatureRepository()
    now = datetime.now(UTC)
    add_outbox(features, now=now)
    claimed = features.claim_telegram_delivery(now=now, lease_until=now + timedelta(seconds=15), claim_id="old-claim")
    assert claimed is not None

    telegram = FakeTelegram()
    worker = TelegramNotificationDeliveryWorker(features, auth, telegram, lease_seconds=15)
    status = asyncio.run(worker.run_once(now=now + timedelta(seconds=16)))

    assert status["sent"] == 1
    row = features.list_notifications("telegram-user")[0]
    assert row["telegram_delivery"]["status"] == "sent"
    assert row["telegram_delivery"]["attempts"] == 2


def test_delivery_is_suppressed_if_user_unlinks_telegram_or_turns_off_alerts():
    auth = InMemoryAuthRepository()
    add_user(auth, chat_id="chat-new")
    features = InMemoryUserFeatureRepository()
    now = datetime.now(UTC)
    add_outbox(features, chat_id="chat-old", now=now)
    worker = TelegramNotificationDeliveryWorker(features, auth, FakeTelegram())

    status = asyncio.run(worker.run_once(now=now))

    assert status["suppressed"] == 1
    assert features.list_notifications("telegram-user")[0]["telegram_delivery"]["status"] == "suppressed"


def test_not_configured_worker_leaves_items_pending():
    auth = InMemoryAuthRepository()
    add_user(auth)
    features = InMemoryUserFeatureRepository()
    now = datetime.now(UTC)
    add_outbox(features, now=now)
    worker = TelegramNotificationDeliveryWorker(features, auth, FakeTelegram(configured=False))

    status = asyncio.run(worker.run_once(now=now))

    assert status["status"] == "not_configured"
    assert status["claimed"] == 0
    assert features.list_notifications("telegram-user")[0]["telegram_delivery"]["status"] == "pending"


def test_deleting_inbox_item_does_not_discard_pending_telegram_job():
    features = InMemoryUserFeatureRepository()
    now = datetime.now(UTC)
    doc = add_outbox(features, now=now)

    assert features.delete_notification(doc["id"], "telegram-user") is True
    assert features.list_notifications("telegram-user") == []
    # The queue row is intentionally retained internally and remains claimable.
    claim = features.claim_telegram_delivery(now=now, lease_until=now + timedelta(seconds=30), claim_id="deliver-after-delete")
    assert claim is not None
    assert claim["id"] == doc["id"]


def test_alert_dispatcher_persists_telegram_outbox_without_inline_send():
    auth = InMemoryAuthRepository()
    add_user(auth, prefs={"on": True, "price": True, "liq": True, "mov": True, "vol": True,
                          "signals": True, "plans": True, "in_app": True, "telegram": True})
    features = InMemoryUserFeatureRepository()
    telegram = FakeTelegram()
    processor = UserAlertProcessor(auth, features, InMemoryJsonCache(), telegram)
    now = datetime.now(UTC)

    made, queued = asyncio.run(processor._notify_user(
        auth.get_user_by_id("telegram-user"), event_id="durable-alert-1", kind="price",
        title="Precio de BTC", message="Se alcanzó tu nivel", symbol="BTCUSDT",
        metadata={"target": 100}, now=now,
    ))

    assert made is True and queued is True
    assert telegram.sent == []  # The alert worker only persists; a separate worker delivers.
    stored = features.list_notifications("telegram-user")[0]
    assert stored["telegram_delivery"]["status"] == "pending"
    assert stored["telegram_delivery"]["chat_id"] == "chat-100"
    assert stored["telegram_delivery"]["text"] == "Precio de BTC\n\nSe alcanzó tu nivel"


def test_repeated_transient_failure_becomes_dead_at_max_attempts():
    class TemporaryFailure(RuntimeError):
        retryable = True

    auth = InMemoryAuthRepository()
    add_user(auth)
    features = InMemoryUserFeatureRepository()
    now = datetime.now(UTC)
    add_outbox(features, now=now)
    telegram = FakeTelegram([TemporaryFailure("first"), TemporaryFailure("second")])
    worker = TelegramNotificationDeliveryWorker(features, auth, telegram, max_attempts=2,
                                               retry_base_seconds=5, retry_max_seconds=60)

    first = asyncio.run(worker.run_once(now=now))
    assert first["retried"] == 1
    second = asyncio.run(worker.run_once(now=now + timedelta(seconds=5)))

    assert second["dead"] == 1
    delivery = features.list_notifications("telegram-user")[0]["telegram_delivery"]
    assert delivery["status"] == "dead"
    assert delivery["attempts"] == 2


def test_delivery_is_suppressed_if_telegram_preferences_are_disabled_after_queueing():
    auth = InMemoryAuthRepository()
    add_user(auth, prefs={"on": True, "liq": True, "mov": True, "vol": True, "price": True,
                          "signals": True, "plans": True, "in_app": True, "telegram": False})
    features = InMemoryUserFeatureRepository()
    now = datetime.now(UTC)
    add_outbox(features, now=now)
    telegram = FakeTelegram()
    worker = TelegramNotificationDeliveryWorker(features, auth, telegram)

    status = asyncio.run(worker.run_once(now=now))

    assert status["suppressed"] == 1
    assert telegram.sent == []
    assert features.list_notifications("telegram-user")[0]["telegram_delivery"]["status"] == "suppressed"
