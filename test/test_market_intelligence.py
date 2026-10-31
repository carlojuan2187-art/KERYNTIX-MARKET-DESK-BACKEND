from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.cache import InMemoryJsonCache
from api.repositories.signals import InMemorySignalRepository
from api.repositories.sweeps import InMemorySweepStore
from api.security import Principal, current_principal, require_analytical_access
from api.services.market import FEED_STATUS_KEY, FUNDING_KEY, QUOTES_KEY
from api.services.market_intelligence import MarketIntelligenceService
from engine.liquidations import interpret_liquidations, summarize_events
from engine.movers import rank_candidates, window_changes
from main import create_app


class FakeRest:
    def __init__(self):
        self.calls = []

    async def fetch_24h_tickers(self):
        self.calls.append(("tickers",))
        return [{"symbol": "BTCUSDT", "lastPrice": "100", "priceChangePercent": "1.5", "quoteVolume": "2000000000"}]

    async def fetch_klines(self, symbol, interval, limit, **kwargs):
        self.calls.append(("klines", symbol, interval, limit))
        step = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}[interval]
        end = int(time.time() * 1000)
        rows = []
        for i in range(limit):
            close = 100 + i * 0.05
            start = end - (limit - i) * step
            rows.append([start, str(close - .02), str(close + .2), str(close - .2), str(close), "1000", start + step - 1, str(close * 1000), "100", "600", str(close * 600), "0"])
        return rows

    async def fetch_endpoint_json(self, path, params=None):
        self.calls.append(("endpoint", path, params))
        return []

    async def close(self):
        return None


def run(coro):
    return asyncio.run(coro)


def test_liquidation_summary_side_semantics_and_reading():
    now = 1_800_000_000.0
    events = [
        {"timestamp": now - 30, "symbol": "ETHUSDT", "side": "long", "price": 2000, "quantity": 500, "amount_usdt": 1_000_000},
        {"timestamp": now - 20, "symbol": "BTCUSDT", "side": "short", "price": 60000, "quantity": 5, "amount_usdt": 300_000},
        {"timestamp": now - 901, "symbol": "OLDUSDT", "side": "long", "price": 10, "quantity": 100, "amount_usdt": 1000},
    ]
    summary = summarize_events(events, 15, now=now)
    assert summary["long_usd"] == 1_000_000
    assert summary["short_usd"] == 300_000
    assert summary["long_n"] == 1 and summary["short_n"] == 1
    assert summary["top"][0]["symbol"] == "ETHUSDT"
    assert any("Barrido de longs" in note for note in interpret_liquidations(summary))


def test_movers_price_history_uses_real_samples_and_volume_filter():
    now = 1_800_000_000.0
    history = [
        {"timestamp": now - 15 * 60, "price": 100, "volume_24h": 50_000_000},
        {"timestamp": now - 7 * 60, "price": 103, "volume_24h": 51_000_000},
        {"timestamp": now - 1, "price": 105, "volume_24h": 52_000_000},
    ]
    row = window_changes(history, now=now, minutes=15, latest_quote={"price": 105, "volume_24h": 52_000_000})
    assert row is not None
    assert row["change_pct"] == pytest.approx(5.0)
    ranked = rank_candidates({"AAAUSDT": row, "LOWUSDT": {**row, "qvol": 1000}}, 15)
    assert ranked["eligible_count"] == 1
    assert ranked["gainers"][0]["symbol"] == "AAAUSDT"


def test_market_intelligence_liquidations_reads_shared_worker_events():
    cache = InMemoryJsonCache()
    now = time.time()
    events = [
        {"timestamp": now - 30, "symbol": "BTCUSDT", "side": "long", "price": 100, "quantity": 1000, "amount_usdt": 100_000},
        {"timestamp": now - 20, "symbol": "ETHUSDT", "side": "short", "price": 100, "quantity": 500, "amount_usdt": 50_000},
    ]
    async def setup():
        for event in events:
            await cache.append_timed_json("market:liquidations:events:v1", event, timestamp=event["timestamp"], max_length=100, retention_seconds=7200)
        await cache.set_json("market:liquidations:status:v1", {"connected": True, "started_at_epoch": now - 1800})
        await cache.set_json(FEED_STATUS_KEY, {"connected": True, "status": "healthy"})
    run(setup())
    service = MarketIntelligenceService(cache, FakeRest(), InMemorySignalRepository([]), InMemorySweepStore())
    data = run(service.liquidations(5))
    assert data["total"] == pytest.approx(150_000)
    assert data["long_n"] == 1 and data["short_n"] == 1
    assert data["connected"] is True
    assert data["coverage_complete"] is True


def test_market_intelligence_movers_returns_collecting_not_fake_rows():
    cache = InMemoryJsonCache()
    now = time.time()
    async def setup():
        await cache.set_json(QUOTES_KEY, {"BTCUSDT": {"symbol": "BTCUSDT", "price": 100, "volume_24h": 2_000_000_000, "change_pct": 1.0}}, ttl_seconds=100)
        await cache.set_json("market:movers:coverage:v1", {"started_at_epoch": now - 30, "collected_minutes": 0.5})
    run(setup())
    service = MarketIntelligenceService(cache, FakeRest(), InMemorySignalRepository([]), InMemorySweepStore())
    data = run(service.movers(15))
    assert data["status"] == "collecting"
    assert data["ready"] is False
    assert data["gainers"] == []


def test_radar_service_returns_auditable_source_and_caches_result():
    cache = InMemoryJsonCache()
    now = time.time()
    async def setup():
        await cache.set_json(QUOTES_KEY, {"BTCUSDT": {"symbol": "BTCUSDT", "price": 100, "volume_24h": 2_000_000_000, "change_pct": 1.0}}, ttl_seconds=100)
        await cache.set_json(FUNDING_KEY, [{"symbol": "BTCUSDT", "lastFundingRate": 0.0001}], ttl_seconds=100)
    run(setup())
    rest = FakeRest()
    service = MarketIntelligenceService(cache, rest, InMemorySignalRepository([]), InMemorySweepStore())
    data = run(service.radar())
    count = len(rest.calls)
    again = run(service.radar())
    assert data["source"] == "binance-futures-klines-and-observed-sweeps"
    assert data["status"] in {"ok", "partial", "degraded"}
    assert "watch" in data and "sweeps" in data and "movers" in data
    trends = data["timeframe_trends"]["BTCUSDT"]
    assert trends["15M"] in {"up", "down", "range", "transition", "volatile"}
    assert trends["1H"] in {"up", "down", "range", "transition", "volatile"}
    assert trends["4H"] in {"up", "down", "range", "transition", "volatile"}
    assert len(rest.calls) == count
    assert again["updated_at"] == data["updated_at"]


def test_radar_includes_premium_visibility_setups_during_full_five_day_trial():
    cache = InMemoryJsonCache()
    run(cache.set_json(QUOTES_KEY, {
        "BTCUSDT": {"symbol": "BTCUSDT", "price": 100, "volume_24h": 2_000_000_000, "change_pct": 1.0},
    }, ttl_seconds=100))
    run(cache.set_json(FUNDING_KEY, [{"symbol": "BTCUSDT", "lastFundingRate": 0.0001}], ttl_seconds=100))
    repo = InMemorySignalRepository([{
        "id": "trial-premium-setup", "symbol": "BTCUSDT", "direction": "LONG",
        "status": "PENDING", "visibility": "premium",
        "created_at": datetime.now(timezone.utc), "entry_price": 100,
        "entry_zone": {"low": 99, "high": 100}, "take_profits": [105],
        "stop_loss": 95, "score": 90,
    }])
    app = create_app(repository=repo, market_cache=cache, market_rest_client=FakeRest(),
                     sweep_store=InMemorySweepStore())
    trial_principal = Principal("trial-user", {
        "sub": "trial-user", "plan": "free",
        "trial_end": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
        "tz": "UTC",
    }, False)
    app.dependency_overrides[current_principal] = lambda: trial_principal
    app.dependency_overrides[require_analytical_access] = lambda: trial_principal

    with TestClient(app) as client:
        response = client.get("/radar")

    assert response.status_code == 200, response.text
    setups = response.json()["setups"]
    assert len(setups) == 1
    assert setups[0]["visibility"] == "premium"
    assert setups[0]["symbol"] == "BTCUSDT"


def test_timeframe_trends_for_unscanned_symbol_are_real_and_cached():
    cache = InMemoryJsonCache()
    rest = FakeRest()
    service = MarketIntelligenceService(cache, rest, InMemorySignalRepository([]), InMemorySweepStore())

    first = run(service.timeframe_trends_for_symbols(["ETHUSDT"]))
    assert set(first["ETHUSDT"]) == {"15M", "1H", "4H"}
    assert all(value in {"up", "down", "range", "transition", "volatile"} for value in first["ETHUSDT"].values())
    count = len(rest.calls)
    second = run(service.timeframe_trends_for_symbols(["ETHUSDT"]))
    assert second == first
    assert len(rest.calls) == count


def test_overview_uses_shared_snapshots_and_counts_signal_statuses():
    cache = InMemoryJsonCache()
    now = time.time()
    repo = InMemorySignalRepository([
        {"id": "1", "symbol": "BTCUSDT", "status": "PENDING", "created_at": datetime.now(timezone.utc)},
        {"id": "2", "symbol": "ETHUSDT", "status": "TOUCHED", "created_at": datetime.now(timezone.utc)},
    ])
    async def setup():
        await cache.set_json(QUOTES_KEY, {
            "BTCUSDT": {"symbol": "BTCUSDT", "price": 100, "volume_24h": 2_000_000_000, "change_pct": 2},
            "ETHUSDT": {"symbol": "ETHUSDT", "price": 10, "volume_24h": 200_000_000, "change_pct": -1},
        }, ttl_seconds=100)
        await cache.set_json(FEED_STATUS_KEY, {"connected": True, "status": "healthy", "last_price_update_at": "2026-10-25T00:00:00Z"})
    run(setup())
    service = MarketIntelligenceService(cache, FakeRest(), repo, InMemorySweepStore())
    data = run(service.overview())
    assert data["status"] == "ok"
    assert data["market"]["asset_count"] == 2
    assert data["signals"]["status_counts"]["PENDING"] == 1
    assert data["signals"]["status_counts"]["TOUCHED"] == 1


def get_test_app():
    cache = InMemoryJsonCache()
    now = time.time()
    run(cache.set_json(QUOTES_KEY, {
        "BTCUSDT": {"symbol": "BTCUSDT", "price": 100, "volume_24h": 2_000_000_000, "change_pct": 1.0},
    }, ttl_seconds=100))
    run(cache.set_json(FUNDING_KEY, [{"symbol": "BTCUSDT", "lastFundingRate": 0.0001}], ttl_seconds=100))
    repo = InMemorySignalRepository([
        {"id": "1", "symbol": "BTCUSDT", "direction": "LONG", "status": "PENDING", "visibility": "free",
         "created_at": datetime.now(timezone.utc), "entry_price": 100, "entry_zone": {"low": 99, "high": 100}, "take_profits": [105], "stop_loss": 95, "score": 70},
    ])
    rest = FakeRest()
    app = create_app(repository=repo, market_cache=cache, market_rest_client=rest,
                     sweep_store=InMemorySweepStore())
    principal = Principal("test-user", {"sub": "test-user", "plan": "free", "tz": "UTC"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    return app, cache


def test_new_intelligence_routes_enforce_windows_and_return_expected_shapes():
    app, cache = get_test_app()
    with TestClient(app) as client:
        invalid_lq = client.get("/liquidations?window=10")
        invalid_movers = client.get("/movers?window=10")
        overview = client.get("/overview")
        radar = client.get("/radar")
        movers = client.get("/movers?window=15")
        lq = client.get("/liquidations?window=15")
    assert invalid_lq.status_code == 422
    assert invalid_movers.status_code == 422
    assert overview.status_code == 200
    assert radar.status_code == 200 and "setups" in radar.json()
    assert radar.json()["setups"][0]["symbol"] == "BTCUSDT"
    timeframe_trends = radar.json()["timeframe_trends"]["BTCUSDT"]
    assert timeframe_trends["15M"] is not None
    assert timeframe_trends["1H"] is not None
    assert timeframe_trends["4H"] is not None
    assert movers.status_code == 200 and movers.json()["status"] == "collecting"
    assert lq.status_code == 200 and "total" in lq.json()
