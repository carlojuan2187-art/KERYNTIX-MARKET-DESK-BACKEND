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
