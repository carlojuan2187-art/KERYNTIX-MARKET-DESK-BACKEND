from __future__ import annotations

import asyncio
import time

from api.cache import InMemoryJsonCache
from api.repositories.signals import InMemorySignalRepository
from api.repositories.sweeps import InMemorySweepStore
from api.services.market_intelligence import MarketIntelligenceService


def run(coro):
    return asyncio.run(coro)


class BlockingTrendRest:
    async def fetch_klines(self, *args, **kwargs):
        await asyncio.Future()


class EmptyTrendRest:
    async def fetch_klines(self, *args, **kwargs):
        return []


def test_active_signal_repository_skips_closed_and_old_cancelled_history():
    now = time.time()
    repository = InMemorySignalRepository([
        {"id": "pending", "created_at": now, "status": "PENDING"},
        {"id": "touched", "created_at": now - 1, "status": "TOUCHED"},
        {"id": "cancelled-recent", "created_at": now - 2, "status": "CANCELLED",
         "cancelled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))},
        {"id": "cancelled-old", "created_at": now - 3, "status": "CANCELLED",
         "cancelled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600))},
        {"id": "closed", "created_at": now - 4, "status": "CLOSED",
         "outcome": {"state": "CLOSED"}},
    ])

    rows = repository.list_active_signals(limit=20)
    assert [row["id"] for row in rows] == ["pending", "touched", "cancelled-recent"]


def test_missing_radar_trends_return_immediately_and_refresh_in_background():
    async def scenario():
        cache = InMemoryJsonCache()
        rest = BlockingTrendRest()
        service = MarketIntelligenceService(
            cache, rest, InMemorySignalRepository([]), InMemorySweepStore()
        )

        result = await asyncio.wait_for(
            service.timeframe_trends_for_symbols(["BTCUSDT"]),
            timeout=0.25,
        )
        assert result == {"BTCUSDT": {"15M": None, "1H": None, "4H": None}}
        task = service._timeframe_trend_tasks.get("BTCUSDT")
        assert task is not None
        assert service.timeframe_trends_refreshing(["BTCUSDT"]) is True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())


def test_incomplete_binance_trend_response_never_overwrites_real_cached_trends():
    async def scenario():
        cache = InMemoryJsonCache()
        key = "market:timeframe-trends:v1:BTCUSDT"
        known = {"15M": "up", "1H": "down", "4H": "range"}
        await cache.set_json(key, {
            "trends": known,
            "updated_at": "2026-11-05T10:00:00Z",
            "updated_epoch": time.time() - 120,
            "source": "binance-futures-klines",
        }, ttl_seconds=1800)
        service = MarketIntelligenceService(
            cache, EmptyTrendRest(), InMemorySignalRepository([]), InMemorySweepStore()
        )

        result = await service._refresh_timeframe_trend("BTCUSDT")
        saved = await cache.get_json(key)
        assert result == known
        assert saved["trends"] == known

    run(scenario())
