import asyncio
from datetime import datetime, timedelta, timezone

from api.repositories.signals import InMemorySignalRepository
from worker.user_alerts import UserAlertProcessor


class FakeAuthRepository:
    def __init__(self, users):
        self.users = users

    def list_users_for_alerts(self, *, limit=500, after_user_id=None):
        if after_user_id is not None:
            return []
        return self.users[:limit]


class FakeFeatureRepository:
    def __init__(self, *, created=True):
        self.created = created

    def active_push_subscription_count(self, user_id):
        return 0

    def create_notification(self, document):
        return dict(document), self.created


class FakeCache:
    async def publish_json(self, channel, payload):
        return None


def test_signal_notification_records_only_users_with_active_access():
    now = datetime.now(timezone.utc)
    active_user = {
        "user_id": "active-user",
        "plan": "free",
        "created_at": now - timedelta(days=2),
        "trial_end": now + timedelta(days=2),
        "alert_prefs": {
            "on": True, "signals": True, "in_app": True,
            "telegram": False, "web_push": False,
        },
    }
    expired_user = {
        "user_id": "expired-user",
        "plan": "free",
        "created_at": now - timedelta(days=10),
        "trial_end": now - timedelta(days=5),
        "alert_prefs": {
            "on": True, "signals": True, "in_app": True,
            "telegram": False, "web_push": False,
        },
    }
    repository = InMemorySignalRepository()
    processor = UserAlertProcessor(
        FakeAuthRepository([active_user, expired_user]),
        FakeFeatureRepository(),
        FakeCache(),
        signal_repository=repository,
    )

    result = asyncio.run(processor.notify_new_signal({
        "id": "signal-123",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "visibility": "premium",
        "entry_price": 100,
        "stop_loss": 95,
        "take_profits": [105, 110],
        "score": 80,
    }, now=now))

    deliveries = repository.list_user_deliveries("active-user")
    assert result["notifications_created"] == 1
    assert [row["signal_id"] for row in deliveries] == ["signal-123"]
    assert repository.list_user_deliveries("expired-user") == []



def test_duplicate_signal_notification_repairs_personal_delivery_ledger():
    now = datetime.now(timezone.utc)
    active_user = {
        "user_id": "active-user",
        "plan": "free",
        "created_at": now - timedelta(days=2),
        "trial_end": now + timedelta(days=2),
        "alert_prefs": {
            "on": True, "signals": True, "in_app": True,
            "telegram": False, "web_push": False,
        },
    }
    repository = InMemorySignalRepository()
    processor = UserAlertProcessor(
        FakeAuthRepository([active_user]),
        FakeFeatureRepository(created=False),
        FakeCache(),
        signal_repository=repository,
    )

    result = asyncio.run(processor.notify_new_signal({
        "id": "signal-duplicate",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "visibility": "premium",
        "entry_price": 100,
        "stop_loss": 95,
        "take_profits": [105, 110],
    }, now=now))

    assert result["notifications_created"] == 0
    assert [row["signal_id"] for row in repository.list_user_deliveries("active-user")] == ["signal-duplicate"]
