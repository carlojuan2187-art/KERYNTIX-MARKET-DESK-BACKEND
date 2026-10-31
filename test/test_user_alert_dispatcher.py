from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.user_features import InMemoryUserFeatureRepository
from api.services.market import QUOTES_KEY
from worker.user_alerts import UserAlertProcessor

UTC = timezone.utc


class NoTelegram:
    def is_configured(self):
        return False

    def send_message(self, *args, **kwargs):
        raise AssertionError("no se debe invocar un Telegram no configurado")


def make_user(repo, user_id="watch-alert-user", *, prefs=None, active=True, chat_id=None, language="en"):
    now = datetime.now(UTC)
    repo.create_user_if_absent({
        "user_id": user_id, "phone": "+1787555" + str(abs(hash(user_id)) % 100000).zfill(5), "telegram_chat_id": chat_id,
        "plan": "premium" if active else "free",
        "plan_end": now + timedelta(days=10) if active else None,
        "trial_end": None, "banned": False, "ref_code": f"ref-{user_id}",
        "alert_prefs": prefs or {"on": True, "liq": True, "mov": True, "vol": True, "price": True, "in_app": True, "telegram": False},
        "language": language,
    })


def test_price_alert_worker_fires_one_shot_once_and_publishes_in_app():
    auth = InMemoryAuthRepository()
    make_user(auth)
    features = InMemoryUserFeatureRepository()
    cache = InMemoryJsonCache()
    alert = features.create_price_alert({
        "id": "pa-1", "user_id": "watch-alert-user", "symbol": "BTCUSDT", "condition": "above",
        "target_price": 100.0, "enabled": True, "repeat": False, "cooldown_seconds": 300,
        "created_at": datetime.now(UTC), "updated_at": datetime.now(UTC), "trigger_count": 0,
    })
    asyncio.run(cache.set_json(QUOTES_KEY, {"BTCUSDT": {"symbol": "BTCUSDT", "price": 105.5}}))
    processor = UserAlertProcessor(auth, features, cache, NoTelegram())
    first = asyncio.run(processor.run_once())
    second = asyncio.run(processor.run_once())
    assert first["price_alerts_triggered"] == 1
    assert first["notifications_created"] == 1
    assert second["price_alerts_triggered"] == 0
    notifications = features.list_notifications("watch-alert-user")
    assert len(notifications) == 1
    assert notifications[0]["kind"] == "price"
    assert notifications[0]["metadata"]["observed_price"] == 105.5
    assert any(channel == "notifications:events:v1" for channel, _event in cache.published)
    assert features.get_price_alert("pa-1", "watch-alert-user")["enabled"] is False


def test_global_liquidation_alert_obeys_preferences_and_idempotency_bucket():
    auth = InMemoryAuthRepository()
    make_user(auth, "liq-user")
    make_user(auth, "muted-user", prefs={"on": False, "liq": True, "mov": True, "vol": True, "in_app": True, "telegram": False})
    features = InMemoryUserFeatureRepository()
    cache = InMemoryJsonCache()
    now = datetime.now(UTC).timestamp()
    asyncio.run(cache.set_json("market:liquidations:summary:v1:5", {
        "status": "ok", "coverage_complete": True, "total": 15_000_000,
        "long_share": 0.82, "top": [{"symbol": "ETHUSDT", "usd": 6_000_000, "long": 5_500_000, "short": 500_000}],
        "updated_epoch": now,
    }))
    processor = UserAlertProcessor(auth, features, cache, NoTelegram(), clock=lambda: now)
    first = asyncio.run(processor.run_once())
    second = asyncio.run(processor.run_once())
    assert first["market_candidates"] == 1
    assert first["notifications_created"] == 1
    assert second["notifications_created"] == 0
    assert len(features.list_notifications("liq-user")) == 1
    assert features.list_notifications("muted-user") == []


def test_market_alerts_skip_incomplete_data_and_estimated_volume():
    auth = InMemoryAuthRepository()
    make_user(auth, "notified-user")
    features = InMemoryUserFeatureRepository()
    cache = InMemoryJsonCache()
    now = datetime.now(UTC).timestamp()
    asyncio.run(cache.set_json("market:liquidations:summary:v1:5", {
        "status": "collecting", "coverage_complete": False, "total": 30_000_000,
        "long_share": 0.95, "top": [], "updated_epoch": now,
    }))
    asyncio.run(cache.set_json("market:movers:computed:v1:5", {
        "ready": True,
        "gainers": [{"symbol": "BTCUSDT", "change_pct": 4.0, "volume_ratio": 5.0, "volume_exact": False}],
        "losers": [], "flat_volume": [],
    }))
    processor = UserAlertProcessor(auth, features, cache, NoTelegram(), clock=lambda: now)
    status = asyncio.run(processor.run_once())
    assert status["market_candidates"] == 0
    assert features.list_notifications("notified-user") == []


def test_alert_dispatcher_refreshes_market_snapshots_without_frontend_requests():
    class FakeIntelligence:
        def __init__(self):
            self.calls = []

        async def liquidations(self, window):
            self.calls.append(("liquidations", window))
            return {"status": "collecting"}

        async def movers(self, window):
            self.calls.append(("movers", window))
            return {"status": "collecting", "ready": False}

    auth = InMemoryAuthRepository()
    features = InMemoryUserFeatureRepository()
    cache = InMemoryJsonCache()
    intelligence = FakeIntelligence()
    processor = UserAlertProcessor(auth, features, cache, NoTelegram(), market_intelligence=intelligence)
    asyncio.run(processor.run_once())
    assert intelligence.calls == [("liquidations", 5), ("movers", 5), ("movers", 15)]


def test_global_market_alert_policy_persists_cooldown_and_allows_double_magnitude_escalation():
    cache = InMemoryJsonCache()
    processor = UserAlertProcessor(InMemoryAuthRepository(), InMemoryUserFeatureRepository(), cache, NoTelegram())
    first = {"kind": "mov", "alert_key": "mov:BTCUSDT:up", "magnitude": 3.5,
             "event_id": "old-id", "title": "Pump", "message": "BTC", "symbol": "BTCUSDT", "metadata": {}}
    base = 1_800_000_000.0
    accepted = asyncio.run(processor._filter_global_market_candidates([first], base))
    assert len(accepted) == 1
    cooling = asyncio.run(processor._filter_global_market_candidates([{**first, "magnitude": 4.0}], base + 600))
    assert cooling == []
    escalated = asyncio.run(processor._filter_global_market_candidates([{**first, "magnitude": 7.0}], base + 600))
    assert len(escalated) == 1
    assert escalated[0]["event_id"] != accepted[0]["event_id"]


def test_global_market_alert_policy_prioritizes_market_liquidations_and_caps_hourly(monkeypatch):
    monkeypatch.setenv("ALERTS_MAX_PER_HOUR", "1")
    cache = InMemoryJsonCache()
    processor = UserAlertProcessor(InMemoryAuthRepository(), InMemoryUserFeatureRepository(), cache, NoTelegram())
    candidates = [
        {"kind": "mov", "alert_key": "mov:ETHUSDT:up", "magnitude": 5.0, "title": "Mover", "message": "ETH"},
        {"kind": "liq", "alert_key": "liq:market", "magnitude": 20_000_000.0, "title": "Liquidations", "message": "Market"},
    ]
    base = 1_800_000_000.0
    accepted = asyncio.run(processor._filter_global_market_candidates(candidates, base))
    assert len(accepted) == 1
    assert accepted[0]["alert_key"] == "liq:market"
    later = asyncio.run(processor._filter_global_market_candidates([
        {"kind": "vol", "alert_key": "vol:BTCUSDT", "magnitude": 8.0, "title": "Volume", "message": "BTC"}
    ], base + 30))
    assert later == []


def test_web_push_only_preference_queues_durable_delivery_from_price_alert():
    auth = InMemoryAuthRepository()
    make_user(auth, prefs={"on": True, "price": True, "in_app": False, "telegram": False, "web_push": True})
    features = InMemoryUserFeatureRepository()
    now = datetime.now(UTC)
    features.create_or_update_push_subscription({
        "id": "web-push-subscription", "user_id": "watch-alert-user",
        "endpoint": "https://fcm.googleapis.com/fcm/send/example", "endpoint_hash": "hash-web-push",
        "p256dh": "A" * 87, "auth": "B" * 22, "active": True,
        "created_at": now, "updated_at": now,
    })
    cache = InMemoryJsonCache()
    features.create_price_alert({
        "id": "pa-web-push", "user_id": "watch-alert-user", "symbol": "BTCUSDT", "condition": "above",
        "target_price": 100.0, "enabled": True, "repeat": False, "cooldown_seconds": 300,
        "created_at": now, "updated_at": now, "trigger_count": 0,
    })
    asyncio.run(cache.set_json(QUOTES_KEY, {"BTCUSDT": {"symbol": "BTCUSDT", "price": 105.5}}))
    processor = UserAlertProcessor(auth, features, cache, NoTelegram(), clock=lambda: now.timestamp())
    outcome = asyncio.run(processor.run_once())
    assert outcome["notifications_created"] == 1
    row = next(iter(features._notifications.values()))  # push-only events are intentionally absent from the in-app inbox.
    assert row["in_app"] is False
    assert row["web_push_delivery"]["status"] == "pending"
    assert "telegram_delivery" not in row



def test_new_signal_creates_durable_notifications_only_for_eligible_opted_in_users():
    auth = InMemoryAuthRepository()
    make_user(auth, "premium-signals", prefs={
        "on": True, "signals": True, "in_app": True, "telegram": True, "web_push": False,
        "liq": True, "mov": True, "vol": True,
    }, chat_id="telegram-premium")
    make_user(auth, "muted-signals", prefs={
        "on": True, "signals": False, "in_app": True, "telegram": True, "web_push": False,
        "liq": True, "mov": True, "vol": True,
    }, chat_id="telegram-muted")
    make_user(auth, "expired-signals", active=False, prefs={
        "on": True, "signals": True, "in_app": True, "telegram": True, "web_push": False,
        "liq": True, "mov": True, "vol": True,
    }, chat_id="telegram-expired")
    features = InMemoryUserFeatureRepository()
    cache = InMemoryJsonCache()
    processor = UserAlertProcessor(auth, features, cache, NoTelegram())
    signal = {
        "id": "signal-abc", "symbol": "BTCUSDT", "direction": "LONG", "visibility": "premium",
        "entry_price": 65000.0, "stop_loss": 64200.0, "take_profits": [66500.0, 67200.0],
        "score": 88.5,
    }

    first = asyncio.run(processor.notify_new_signal(signal))
    replay = asyncio.run(processor.notify_new_signal(signal))

    assert first["users_scanned"] == 3
    assert first["notifications_created"] == 1
    assert first["telegram_queued"] == 1
    assert replay["notifications_created"] == 0
    notifications = features.list_notifications("premium-signals")
    assert len(notifications) == 1
    assert notifications[0]["kind"] == "signals"
    assert notifications[0]["event_id"] == "signal-created:signal-abc"
    assert notifications[0]["telegram_delivery"]["status"] == "pending"
    assert "BTCUSDT" in notifications[0]["message"]
    assert features.list_notifications("muted-signals") == []
    assert features.list_notifications("expired-signals") == []
    assert any(channel == "notifications:events:v1" for channel, _event in cache.published)


def test_public_signal_notification_can_reach_an_active_non_premium_user():
    auth = InMemoryAuthRepository()
    now = datetime.now(UTC)
    auth.create_user_if_absent({
        "user_id": "trial-public", "phone": "+17875550001", "plan": "free", "plan_end": None,
        "trial_end": now + timedelta(days=2), "banned": False, "ref_code": "trial-public",
        "alert_prefs": {"on": True, "signals": True, "in_app": True, "telegram": False, "web_push": False},
    })
    features = InMemoryUserFeatureRepository()
    processor = UserAlertProcessor(auth, features, InMemoryJsonCache(), NoTelegram())
    outcome = asyncio.run(processor.notify_new_signal({
        "id": "public-signal", "symbol": "ETHUSDT", "direction": "SHORT", "visibility": "public",
    }, now=now))
    assert outcome["notifications_created"] == 1
    assert len(features.list_notifications("trial-public")) == 1


def test_new_signal_notifications_follow_each_users_language_preference():
    auth = InMemoryAuthRepository()
    signal_prefs = {
        "on": True, "signals": True, "in_app": True, "telegram": True, "web_push": False,
        "liq": True, "mov": True, "vol": True,
    }
    make_user(auth, "signals-english", prefs=signal_prefs, active=True, chat_id="chat-en", language="en")
    make_user(auth, "signals-spanish", prefs=signal_prefs, active=True, chat_id="chat-es", language="es")
    features = InMemoryUserFeatureRepository()
    cache = InMemoryJsonCache()
    processor = UserAlertProcessor(auth, features, cache, NoTelegram())

    result = asyncio.run(processor.notify_new_signal({
        "id": "signal-i18n", "symbol": "BTCUSDT", "direction": "LONG", "visibility": "premium",
        "entry_price": 65000.0, "stop_loss": 64000.0, "take_profits": [67000.0], "score": 90,
    }))

    assert result["notifications_created"] == 2
    english = features.list_notifications("signals-english")[0]
    spanish = features.list_notifications("signals-spanish")[0]
    assert english["title"] == "New LONG signal · BTCUSDT"
    assert "FTrade published a new LONG signal" in english["message"]
    assert "New LONG signal" in english["telegram_delivery"]["text"]
    assert spanish["title"] == "Nueva señal LONG · BTCUSDT"
    assert "FTrade publicó una nueva señal" in spanish["message"]
    assert "Nueva señal LONG" in spanish["telegram_delivery"]["text"]
