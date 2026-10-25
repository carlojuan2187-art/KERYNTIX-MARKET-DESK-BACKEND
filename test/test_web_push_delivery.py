from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.user_features import InMemoryUserFeatureRepository
from api.services.web_push import WebPushSendError
from worker.web_push_delivery import WebPushDeliveryWorker

UTC = timezone.utc


class FakePushGateway:
    def __init__(self, outcomes=None, *, configured=True):
        self.outcomes = list(outcomes or [])
        self.configured = configured
        self.sent = []

    def is_configured(self):
        return self.configured

    async def send(self, subscription, payload, *, ttl_seconds=300):
        self.sent.append((subscription["id"], dict(payload), ttl_seconds))
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome


def add_user(auth, user_id="push-user", *, prefs=None, active=True):
    now = datetime.now(UTC)
    auth.create_user_if_absent({
        "user_id": user_id, "phone": "+17875550201", "telegram_chat_id": None,
        "plan": "premium" if active else "free", "plan_end": now + timedelta(days=30) if active else None,
        "trial_end": None, "banned": False, "ref_code": f"ref-{user_id}",
        "alert_prefs": prefs or {"on": True, "web_push": True, "in_app": True, "telegram": False,
                                  "liq": True, "mov": True, "vol": True, "signals": True, "price": True, "plans": True},
    })


def add_subscription(features, user_id="push-user", *, suffix="1"):
    now = datetime.now(UTC)
    return features.create_or_update_push_subscription({
        "id": f"sub-{suffix}", "user_id": user_id, "endpoint": f"https://fcm.googleapis.com/fcm/send/{suffix}",
        "endpoint_hash": f"hash-{suffix}", "p256dh": "A" * 87, "auth": "B" * 22,
        "user_agent": None, "active": True, "created_at": now, "updated_at": now,
    })


def add_outbox(features, *, user_id="push-user", event_id="event-1", kind="price", now=None):
    now = now or datetime.now(UTC)
    return features.create_notification({
        "id": f"n-{event_id}", "user_id": user_id, "event_id": event_id, "kind": kind,
        "title": "BTC tocó objetivo", "message": "Se activó una alerta", "symbol": "BTCUSDT",
        "metadata": {"price": 100}, "created_at": now, "read_at": None, "in_app": True,
        "web_push_delivery": {"status": "pending", "attempts": 0, "endpoint_results": {},
                              "next_attempt_at": now, "created_at": now, "updated_at": now},
    })[0]


def build_worker(features, auth, gateway, now, **kwargs):
    kwargs.setdefault("retry_base_seconds", 5)
    kwargs.setdefault("retry_max_seconds", 60)
    return WebPushDeliveryWorker(features, auth, gateway, InMemoryJsonCache(), clock=lambda: now, **kwargs)


def test_web_push_outbox_sends_and_marks_notification_sent():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository(); add_user(auth)
    features = InMemoryUserFeatureRepository(); add_subscription(features); add_outbox(features, now=now)
    gateway = FakePushGateway()
    status = asyncio.run(build_worker(features, auth, gateway, now).run_once(now=now))
    assert status["claimed"] == 1 and status["sent"] == 1 and status["endpoints_sent"] == 1
    assert gateway.sent[0][1]["title"] == "BTC tocó objetivo"
    assert gateway.sent[0][1]["url"] == "/"
    row = features.list_notifications("push-user")[0]
    assert row["web_push_delivery"]["status"] == "sent"
    assert row["web_push_delivery"]["endpoint_results"]["sub-1"]["status"] == "sent"


def test_transient_error_retries_only_the_failed_endpoint_not_a_previously_sent_one():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository(); add_user(auth)
    features = InMemoryUserFeatureRepository(); add_subscription(features, suffix="1"); add_subscription(features, suffix="2")
    add_outbox(features, now=now)
    gateway = FakePushGateway([None, WebPushSendError("PUSH_TRANSIENT_503", retryable=True), None])
    worker = build_worker(features, auth, gateway, now)
    first = asyncio.run(worker.run_once(now=now))
    assert first["retried"] == 1
    row = features.list_notifications("push-user")[0]
    assert row["web_push_delivery"]["status"] == "retry"
    second = asyncio.run(worker.run_once(now=now + timedelta(seconds=5)))
    assert second["sent"] == 1
    # Repository ordering is timestamp-based, so either subscription may be the
    # transient one. Exactly one endpoint is attempted once, the other twice.
    from collections import Counter
    call_counts = Counter(item[0] for item in gateway.sent)
    assert sorted(call_counts.values()) == [1, 2]
    row = features.list_notifications("push-user")[0]
    assert {v["status"] for v in row["web_push_delivery"]["endpoint_results"].values()} == {"sent"}


def test_expired_push_endpoint_is_removed_and_subscription_is_not_retried():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository(); add_user(auth)
    features = InMemoryUserFeatureRepository(); add_subscription(features); add_outbox(features, now=now)
    gateway = FakePushGateway([WebPushSendError("PUSH_ENDPOINT_GONE_410", retryable=False, expired=True)])
    status = asyncio.run(build_worker(features, auth, gateway, now).run_once(now=now))
    assert status["suppressed"] == 1 and status["endpoints_expired"] == 1
    assert features.active_push_subscription_count("push-user") == 0
    assert features.list_notifications("push-user")[0]["web_push_delivery"]["status"] == "suppressed"


def test_preference_or_entitlement_disable_suppresses_push_without_calling_gateway():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository(); add_user(auth, prefs={"on": True, "web_push": False, "price": True})
    features = InMemoryUserFeatureRepository(); add_subscription(features); add_outbox(features, now=now)
    gateway = FakePushGateway()
    status = asyncio.run(build_worker(features, auth, gateway, now).run_once(now=now))
    assert status["suppressed"] == 1
    assert gateway.sent == []
    assert features.list_notifications("push-user")[0]["web_push_delivery"]["status"] == "suppressed"


def test_missing_vapid_configuration_does_not_claim_or_discard_outbox():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository(); add_user(auth)
    features = InMemoryUserFeatureRepository(); add_subscription(features); add_outbox(features, now=now)
    gateway = FakePushGateway(configured=False)
    status = asyncio.run(build_worker(features, auth, gateway, now).run_once(now=now))
    assert status["status"] == "not_configured" and status["claimed"] == 0
    assert features.list_notifications("push-user")[0]["web_push_delivery"]["status"] == "pending"


def test_no_subscriptions_suppresses_stale_push_task():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository(); add_user(auth)
    features = InMemoryUserFeatureRepository(); add_outbox(features, now=now)
    gateway = FakePushGateway()
    status = asyncio.run(build_worker(features, auth, gateway, now).run_once(now=now))
    assert status["suppressed"] == 1
    assert features.list_notifications("push-user")[0]["web_push_delivery"]["status"] == "suppressed"


def test_retry_after_from_push_gateway_is_respected_with_a_bounded_delay():
    now = datetime.now(UTC)
    auth = InMemoryAuthRepository(); add_user(auth)
    features = InMemoryUserFeatureRepository(); add_subscription(features); add_outbox(features, now=now)
    gateway = FakePushGateway([WebPushSendError("PUSH_TRANSIENT_429", retryable=True, retry_after_seconds=30)])
    status = asyncio.run(build_worker(features, auth, gateway, now, retry_max_seconds=60).run_once(now=now))
    assert status["retried"] == 1
    delivery = features.list_notifications("push-user")[0]["web_push_delivery"]
    endpoint = delivery["endpoint_results"]["sub-1"]
    assert endpoint["next_attempt_at"] == now + timedelta(seconds=30)
