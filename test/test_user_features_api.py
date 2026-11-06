from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.signals import InMemorySignalRepository
from api.repositories.user_features import InMemoryUserFeatureRepository
from api.security import Principal, current_principal, require_analytical_access
from api.services.user_features import WatchlistService
from main import create_app

UTC = timezone.utc


class FakeMarket:
    def __init__(self):
        self.data = {
            "BTCUSDT": {"symbol": "BTCUSDT", "price": 100.0, "change_pct": 1.2, "volume_24h": 1_000_000,
                        "funding_rate": 0.0001, "trend": {"15M": "UP", "1H": "UP", "4H": None},
                        "score": None, "liquidity_sweep": None},
            "ETHUSDT": {"symbol": "ETHUSDT", "price": 20.0, "change_pct": -0.3, "volume_24h": 500_000,
                        "funding_rate": None, "trend": {"15M": None, "1H": None, "4H": None},
                        "score": None, "liquidity_sweep": None},
        }

    async def quotes(self):
        return list(self.data.values()), SimpleNamespace(status="healthy", updated_at="2026-10-25T12:00:00Z")


def setup():
    auth = InMemoryAuthRepository()
    user_id = "usr_features_1"
    now = datetime.now(UTC)
    auth.create_user_if_absent({
        "user_id": user_id, "phone": "+17875550123", "telegram_chat_id": "777001",
        "plan": "premium", "plan_end": now + timedelta(days=30), "trial_end": None,
        "ref_code": "features-test", "banned": False, "watchlist": [],
        "alert_prefs": {"on": True, "liq": True, "mov": True, "vol": True},
    })
    auth.create_user_if_absent({
        "user_id": "usr_features_2", "phone": "+17875550124", "telegram_chat_id": None,
        "plan": "premium", "plan_end": now + timedelta(days=30), "trial_end": None,
        "ref_code": "features-test-2", "banned": False, "watchlist": [],
    })
    cache = InMemoryJsonCache()
    features = InMemoryUserFeatureRepository()
    app = create_app(InMemorySignalRepository([]), market_cache=cache, auth_repository=auth,
                     user_feature_repository=features)
    market = FakeMarket()
    app.state.watchlist_service.market = market
    app.state.price_alert_service.market = market
    principal = Principal(user_id, {"sub": user_id, "plan": "premium", "plan_end": (now + timedelta(days=30)).isoformat()}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    return app, auth, features, cache, principal



def test_empty_watchlist_returns_without_market_or_signal_history_queries():
    app, _auth, _features, _cache, _principal = setup()

    class NoMarketCalls:
        async def quotes(self):
            raise AssertionError("empty watchlist must not fetch market quotes")

    class NoSignalHistoryCalls:
        def list_signals(self, *, limit=100):
            raise AssertionError("empty watchlist must not query signal history")

    app.state.watchlist_service.market = NoMarketCalls()
    app.state.watchlist_service.signals = NoSignalHistoryCalls()
    with TestClient(app) as client:
        response = client.get("/watchlist")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["status"] == "empty"

def test_watchlist_add_list_remove_and_normalization():
    app, auth, _features, _cache, principal = setup()
    with TestClient(app) as client:
        add = client.post("/watchlist", json={"symbol": "btc"})
        assert add.status_code == 201, add.text
        assert add.json()["total"] == 1
        assert add.json()["items"][0]["symbol"] == "BTCUSDT"
        assert add.json()["items"][0]["price"] == 100.0
        duplicate = client.post("/watchlist", json={"symbol": "BTC/USDT"})
        assert duplicate.status_code == 409
        listed = client.get("/watchlist")
        assert listed.status_code == 200 and listed.json()["total"] == 1
        removed = client.delete("/watchlist/BTCUSDT")
        assert removed.status_code == 200 and removed.json()["total"] == 0


def test_watchlist_rejects_unknown_symbol_and_enforces_limit():
    app, auth, _features, _cache, principal = setup()
    with TestClient(app) as client:
        unknown = client.post("/watchlist", json={"symbol": "UNKNOWN"})
        assert unknown.status_code == 422
        user = auth.get_user_by_id(principal.user_id)
        auth.update_user(principal.user_id, {"watchlist": [f"COIN{i}USDT" for i in range(10)]})
        full = client.post("/watchlist", json={"symbol": "BTCUSDT"})
        assert full.status_code == 409 and full.json()["code"] == "WATCHLIST_FULL"


def test_alert_preferences_patch_preserves_other_values_and_validates_fields():
    app, _auth, _features, _cache, _principal = setup()
    with TestClient(app) as client:
        before = client.get("/account/alert-preferences")
        assert before.status_code == 200
        assert before.json()["preferences"]["liq"] is True
        patched = client.patch("/account/alert-preferences", json={"liq": False, "in_app": False})
        assert patched.status_code == 200
        prefs = patched.json()["preferences"]
        assert prefs["liq"] is False and prefs["in_app"] is False
        assert prefs["mov"] is True
        invalid = client.patch("/account/alert-preferences", json={"made_up": True})
        assert invalid.status_code == 422


def test_price_alert_crud_and_owner_isolation():
    app, _auth, _features, _cache, principal = setup()
    with TestClient(app) as client:
        created = client.post("/alerts/price", json={"symbol": "BTC", "condition": "above", "target_price": 101})
        assert created.status_code == 201, created.text
        alert_id = created.json()["id"]
        assert created.json()["symbol"] == "BTCUSDT"
        listed = client.get("/alerts/price")
        assert listed.status_code == 200 and len(listed.json()) == 1
        patched = client.patch(f"/alerts/price/{alert_id}", json={"enabled": False, "repeat": True})
        assert patched.status_code == 200 and patched.json()["enabled"] is False
        missing = client.delete("/alerts/price/not-yours")
        assert missing.status_code == 404
        deleted = client.delete(f"/alerts/price/{alert_id}")
        assert deleted.status_code == 204


def test_notification_inbox_delete_all_is_user_scoped_and_preserves_pending_delivery():
    app, _auth, features, _cache, principal = setup()
    now = datetime.now(UTC)
    for row in [
        {"id": "clear-1", "user_id": principal.user_id, "event_id": "clear-e1", "kind": "mov", "title": "One", "message": "First", "created_at": now, "read_at": None, "in_app": True},
        {"id": "clear-2", "user_id": principal.user_id, "event_id": "clear-e2", "kind": "liq", "title": "Two", "message": "Second", "created_at": now, "read_at": now, "in_app": True},
        {"id": "clear-pending-telegram", "user_id": principal.user_id, "event_id": "clear-e3", "kind": "mov", "title": "Queued Telegram", "message": "Pending", "created_at": now, "read_at": None, "in_app": True,
         "telegram_delivery": {"status": "pending"}},
        {"id": "clear-pending-push", "user_id": principal.user_id, "event_id": "clear-e4", "kind": "mov", "title": "Queued Push", "message": "Pending", "created_at": now, "read_at": None, "in_app": True,
         "web_push_delivery": {"status": "retry"}},
        {"id": "clear-other-user", "user_id": "usr_features_2", "event_id": "clear-e5", "kind": "mov", "title": "Other", "message": "Keep", "created_at": now, "read_at": None, "in_app": True},
    ]:
        features.create_notification(row)

    with TestClient(app) as client:
        cleared = client.delete("/notifications")
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["deleted"] == 4
        assert cleared.json()["unread_count"] == 0
        assert client.get("/notifications").json()["items"] == []

    # In-flight Telegram/Web Push records remain in storage for their delivery workers.
    claim = features.claim_telegram_delivery(now=now, lease_until=now + timedelta(seconds=30), claim_id="after-clear")
    assert claim is not None and claim["id"] == "clear-pending-telegram"
    assert features._notifications["clear-pending-push"]["in_app"] is False
    assert features._notifications["clear-pending-push"]["web_push_delivery"]["status"] == "retry"
    assert features.list_notifications("usr_features_2")[0]["id"] == "clear-other-user"


def test_notification_inbox_read_all_delete_and_user_isolation():
    app, _auth, features, _cache, principal = setup()
    now = datetime.now(UTC)
    features.create_notification({"id": "n-1", "user_id": principal.user_id, "event_id": "e-1", "kind": "mov",
                                  "title": "Pump", "message": "BTC", "symbol": "BTCUSDT", "metadata": {},
                                  "created_at": now, "read_at": None, "in_app": True})
    features.create_notification({"id": "n-2", "user_id": principal.user_id, "event_id": "e-2", "kind": "liq",
                                  "title": "Liq", "message": "ETH", "symbol": "ETHUSDT", "metadata": {},
                                  "created_at": now + timedelta(seconds=1), "read_at": None, "in_app": True})
    features.create_notification({"id": "other", "user_id": "usr_features_2", "event_id": "e-1", "kind": "mov",
                                  "title": "Private", "message": "not yours", "symbol": "BTCUSDT", "metadata": {},
                                  "created_at": now, "read_at": None, "in_app": True})
    with TestClient(app) as client:
        listed = client.get("/notifications")
        assert listed.status_code == 200
        assert listed.json()["unread_count"] == 2
        assert {row["id"] for row in listed.json()["items"]} == {"n-1", "n-2"}
        marked = client.post("/notifications/n-1/read")
        assert marked.status_code == 200 and marked.json()["unread_count"] == 1
        all_read = client.post("/notifications/read-all")
        assert all_read.status_code == 200 and all_read.json()["unread_count"] == 0
        assert client.delete("/notifications/other").status_code == 404
        assert client.delete("/notifications/n-2").status_code == 204


class FakeKlineRest:
    def __init__(self):
        self.calls = []

    async def fetch_klines(self, symbol, interval, limit):
        import math
        self.calls.append((symbol, interval, limit))
        step_ms = {"15m": 15 * 60_000, "1h": 60 * 60_000, "4h": 4 * 60 * 60_000}[interval]
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        rows = []
        prior = 100.0
        for i in range(240):
            close = 100 + i * 0.02 + math.sin(i / 7) * 0.8
            opened = prior
            high = max(opened, close) + 0.5
            low = min(opened, close) - 0.5
            close_time = now_ms - (240 - i) * step_ms
            rows.append([close_time - step_ms + 1, str(opened), str(high), str(low), str(close),
                         str(100 + (i % 13) * 4), close_time, "20000", 120, "50", "10000", "0"])
            prior = close
        return rows[-limit:]


def test_watchlist_uses_bot_multitimeframe_analysis_and_shared_cache():
    import time

    app, _auth, _features, _cache, _principal = setup()
    rest = FakeKlineRest()
    market = app.state.watchlist_service.market
    market.rest = rest
    with TestClient(app) as client:
        added = client.post("/watchlist", json={"symbol": "btc"})
        assert added.status_code == 201, added.text
        initial = added.json()["items"][0]
        assert set(initial["analyses"]) == {"15m", "1h", "4h"}
        # New symbols respond immediately while the 3 timeframe analyses warm in
        # the background. Poll the API until that shared cache has been populated.
        assert added.json()["analysis_status"] in {"collecting", "ready"}
        deadline = time.monotonic() + 3.0
        ready = None
        while time.monotonic() < deadline:
            response = client.get("/watchlist")
            assert response.status_code == 200, response.text
            payload = response.json()
            if payload["analysis_status"] == "ready" and payload["items"][0]["analyses"]["1h"] is not None:
                ready = payload
                break
            time.sleep(0.05)

        assert ready is not None, "watchlist analysis did not finish its background refresh"
        first = ready["items"][0]
        assert first["analyses"]["1h"] is not None
        assert first["score_detail"] is not None
        assert first["score"] == first["score_detail"]["score"]
        assert first["trend"]["1H"] is not None
        # After the first refresh, requests consume shared cache rather than
        # downloading the 3 timeframe series again.
        listed = client.get("/watchlist")
        assert listed.status_code == 200
        assert len(rest.calls) == 3
        assert listed.json()["analysis_status"] == "ready"


def test_reenabling_price_alert_rejects_duplicate_active_level():
    app, _auth, _features, _cache, _principal = setup()
    with TestClient(app) as client:
        first = client.post("/alerts/price", json={"symbol": "BTC", "condition": "above", "target_price": 101})
        assert first.status_code == 201
        first_id = first.json()["id"]
        assert client.patch(f"/alerts/price/{first_id}", json={"enabled": False}).status_code == 200
        second = client.post("/alerts/price", json={"symbol": "BTC", "condition": "above", "target_price": 101})
        assert second.status_code == 201
        response = client.patch(f"/alerts/price/{first_id}", json={"enabled": True})
        assert response.status_code == 409
        assert response.json()["code"] == "PRICE_ALERT_DUPLICATE"
        current_alerts = client.get("/alerts/price").json()
        states = {row["id"]: row["enabled"] for row in current_alerts}
        assert states[first_id] is False
        assert states[second.json()["id"]] is True


def test_watchlist_returns_active_signal_entry_stop_and_targets():
    app, _auth, _features, _cache, _principal = setup()
    app.state.watchlist_service.signals = InMemorySignalRepository([
        {
            "id": "signal-levels-1", "symbol": "BTCUSDT", "direction": "LONG",
            "status": "PENDING", "score": 91, "entry_price": 100.0,
            "entry_zone": {"low": 99.5, "high": 100.5}, "stop_loss": 97.0,
            "take_profits": [103.0, 106.0, 109.0],
            "created_at": datetime.now(UTC).isoformat(),
        }
    ])
    with TestClient(app) as client:
        response = client.post("/watchlist", json={"symbol": "BTC"})
        assert response.status_code == 201, response.text
        active = response.json()["items"][0]["active_signal"]
        assert active["entry_price"] == 100.0
        assert active["entry_low"] == 99.5 and active["entry_high"] == 100.5
        assert active["stop_loss"] == 97.0
        assert active["take_profits"] == [103.0, 106.0, 109.0]
