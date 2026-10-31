from __future__ import annotations

import asyncio

from api.routes.events import _event_frames


def run(coro):
    return asyncio.run(coro)


def test_price_events_forward_live_market_fields_to_clients():
    payload = [{
        "symbol": "BTCUSDT", "price": 68001.25, "change_pct": 1.7,
        "volume_24h": 123456789.0, "high_24h": 69000.0, "low_24h": 65000.0,
    }]
    frames = run(_event_frames(None, "market:events", {
        "event": "price.update", "payload": payload, "ts": "2026-10-26T12:00:00Z",
    }, "user-1", True, {}))
    assert len(frames) == 1
    assert frames[0]["channel"] == "prices"
    assert frames[0]["event"] == "tick"
    assert frames[0]["payload"] == payload[0]


def test_funding_updates_are_forwarded_as_live_price_channel_frames():
    payload = [{
        "symbol": "BTCUSDT", "lastFundingRate": 0.0001,
        "markPrice": 68002.0, "indexPrice": 67998.0, "nextFundingTime": 1761504000000,
    }]
    frames = run(_event_frames(None, "market:events", {
        "event": "funding.update", "payload": payload, "ts": "2026-10-26T12:00:00Z",
    }, "user-1", True, {}))
    assert len(frames) == 1
    assert frames[0]["event"] == "funding.tick"
    assert frames[0]["payload"]["symbol"] == "BTCUSDT"
    assert frames[0]["payload"]["funding_rate"] == 0.0001


def test_price_events_are_not_throttled_a_second_time_per_symbol():
    # The browser transport must not discard a new price just because a prior tick
    # for the same symbol was forwarded less than one second ago.
    last_ticks = {"BTCUSDT": 999999999.0}
    payload = [{"symbol": "BTCUSDT", "price": 68002.0}]
    frames = run(_event_frames(None, "market:events", {
        "event": "price.update", "payload": payload, "ts": "2026-10-26T12:00:00Z",
    }, "user-1", True, last_ticks))
    assert len(frames) == 1
    assert frames[0]["payload"]["price"] == 68002.0


def test_expired_account_does_not_receive_market_notifications_over_websocket():
    frames = run(_event_frames(None, "notifications:events:v1", {
        "user_id": "expired-user",
        "notification": {"id": "n-1", "kind": "signals", "message": "Premium setup"},
    }, "expired-user", False, {}))
    assert frames == []


def test_expired_account_can_still_receive_account_events_needed_for_billing():
    frames = run(_event_frames(None, "account:events:v1", {
        "user_id": "expired-user", "channel": "account", "event": "payment.updated",
        "payload": {"status": "completed"},
    }, "expired-user", False, {}))
    assert len(frames) == 1
    assert frames[0]["channel"] == "account"
    assert frames[0]["event"] == "payment.updated"


def test_expired_account_only_receives_plan_notifications_not_unknown_feature_alerts():
    plan_frames = run(_event_frames(None, "notifications:events:v1", {
        "user_id": "expired-user",
        "notification": {"id": "n-plan", "kind": "plans", "message": "Renew Premium"},
    }, "expired-user", False, {}))
    unknown_frames = run(_event_frames(None, "notifications:events:v1", {
        "user_id": "expired-user",
        "notification": {"id": "n-unknown", "kind": "new_feature", "message": "Feature alert"},
    }, "expired-user", False, {}))
    assert len(plan_frames) == 1
    assert unknown_frames == []


def test_full_five_day_trial_can_receive_premium_signal_websocket_events(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from api.routes.events import _event_frames
    from domain.plans import evaluate_access

    async def load_signal(_request, signal_id):
        return {"id": signal_id, "symbol": "BTCUSDT", "direction": "LONG", "visibility": "premium"}

    monkeypatch.setattr("api.routes.events._find_signal", load_signal)
    trial_user = {
        "plan": "free",
        "trial_end": datetime.now(timezone.utc) + timedelta(days=5),
    }
    entitlement = evaluate_access(trial_user)
    assert entitlement["allowed"] is True
    assert entitlement["trial_active"] is True

    frames = run(_event_frames(
        None, "signals:new:v1", {"id": "premium-during-trial"},
        "trial-user", bool(entitlement["allowed"]), {},
    ))
    assert len(frames) == 1
    assert frames[0]["channel"] == "signals"
    assert frames[0]["payload"]["visibility"] == "premium"


def test_expired_trial_does_not_resolve_or_receive_premium_signal_websocket_events(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from api.routes.events import _event_frames
    from domain.plans import evaluate_access

    looked_up = False

    async def load_signal(_request, signal_id):
        nonlocal looked_up
        looked_up = True
        return {"id": signal_id, "symbol": "BTCUSDT", "direction": "LONG", "visibility": "premium"}

    monkeypatch.setattr("api.routes.events._find_signal", load_signal)
    expired_user = {
        "plan": "free",
        "trial_end": datetime.now(timezone.utc) - timedelta(seconds=1),
    }
    entitlement = evaluate_access(expired_user)
    assert entitlement["allowed"] is False
    assert entitlement["status"] == "expired"

    frames = run(_event_frames(
        None, "signals:new:v1", {"id": "premium-after-trial"},
        "expired-user", bool(entitlement["allowed"]), {},
    ))
    assert frames == []
    assert looked_up is False
