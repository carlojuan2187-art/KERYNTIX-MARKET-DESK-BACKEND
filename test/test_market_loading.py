from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from api.cache import InMemoryJsonCache
from api.services.market import FEED_STATUS_KEY, QUOTES_KEY, MarketDataService, quote_from_ticker
from api.services.user_features import WatchlistService


def run(coro):
    return asyncio.run(coro)


class BlockingMarketRest:
    def __init__(self):
        self.started = asyncio.Event()

    async def fetch_klines(self, *args, **kwargs):
        self.started.set()
        await asyncio.Future()


class BlockingQuotesRest:
    def __init__(self):
        self.started = asyncio.Event()

    async def fetch_24h_tickers(self):
        self.started.set()
        await asyncio.Future()

    async def fetch_funding(self):
        await asyncio.Future()

    async def close(self):
        return None


def test_watchlist_cold_analysis_returns_without_waiting_for_binance():
    async def scenario():
        cache = InMemoryJsonCache()
        rest = BlockingMarketRest()
        service = WatchlistService(
            auth_repository=None,
            market_service=SimpleNamespace(rest=rest),
            cache=cache,
            signal_repository=None,
        )

        result = await asyncio.wait_for(service._symbol_analysis("BTCUSDT"), timeout=0.25)
        assert set(result) == {"15m", "1h", "4h"}
        assert all(value is None for value in result.values())

        task = service._analysis_refresh_tasks.get("BTCUSDT")
        assert task is not None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())


def test_stale_market_quotes_are_returned_before_rest_refresh_finishes():
    async def scenario():
        now = time.time()
        cache = InMemoryJsonCache()
        quote = quote_from_ticker({
            "s": "BTCUSDT", "c": "70000", "o": "69000", "h": "71000",
            "l": "68000", "q": "123456789",
        })
        await cache.set_json(QUOTES_KEY, {"BTCUSDT": quote}, ttl_seconds=7200)
        await cache.set_json(FEED_STATUS_KEY, {
            "connected": False,
            "source": "websocket",
            "status": "degraded",
            "last_price_update_epoch": now - 120,
            "last_price_update_at": "2026-11-05T10:00:00Z",
            "updated_at": "2026-11-05T10:00:00Z",
            "message": "feed stale",
        }, ttl_seconds=7200)
        rest = BlockingQuotesRest()
        service = MarketDataService(cache, rest)

        rows, status = await asyncio.wait_for(service.quotes(), timeout=0.25)
        assert rows and rows[0].symbol == "BTCUSDT"
        assert status.status == "degraded"
        assert status.source == "cache"

        task = service._quotes_refresh_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    run(scenario())


def test_signal_rows_are_shared_and_reused_across_screen_requests():
    from api.repositories.signals import InMemorySignalRepository
    from api.repositories.sweeps import InMemorySweepStore
    from api.services.market_intelligence import MarketIntelligenceService

    class CountingRepository(InMemorySignalRepository):
        def __init__(self):
            super().__init__([{
                "id": "signal-1", "symbol": "BTCUSDT", "status": "PENDING",
                "visibility": "premium", "created_at": "2026-11-05T12:00:00Z",
            }])
            self.read_count = 0

        def list_live_candidates(self, *, limit=80, cancelled_since=None):
            self.read_count += 1
            return super().list_live_candidates(limit=limit, cancelled_since=cancelled_since)

    async def scenario():
        cache = InMemoryJsonCache()
        repository = CountingRepository()
        service = MarketIntelligenceService(cache, BlockingMarketRest(), repository, InMemorySweepStore())
        second_instance = MarketIntelligenceService(cache, BlockingMarketRest(), repository, InMemorySweepStore())
        first, refreshing, updated_at = await service.signal_records(wait_seconds=1.0)
        assert not refreshing
        assert first and first[0]["symbol"] == "BTCUSDT"
        assert updated_at
        second, refreshing_again, updated_at_again = await second_instance.signal_records()
        assert second == first
        assert not refreshing_again
        assert updated_at_again == updated_at
        assert repository.read_count == 1

    run(scenario())


def test_missing_radar_trends_return_immediately_while_provider_is_slow():
    from api.repositories.signals import InMemorySignalRepository
    from api.repositories.sweeps import InMemorySweepStore
    from api.services.market_intelligence import MarketIntelligenceService

    class SlowTrendRest:
        def __init__(self):
            self.started = asyncio.Event()

        async def fetch_klines(self, *args, **kwargs):
            self.started.set()
            await asyncio.Future()

    async def scenario():
        cache = InMemoryJsonCache()
        rest = SlowTrendRest()
        service = MarketIntelligenceService(cache, rest, InMemorySignalRepository([]), InMemorySweepStore())
        result = await asyncio.wait_for(service.timeframe_trends_for_symbols(["ETHUSDT"]), timeout=0.2)
        assert result["ETHUSDT"] == {"15M": None, "1H": None, "4H": None}
        task = service._timeframe_trend_refresh_tasks.get("ETHUSDT")
        assert task is not None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    run(scenario())


def test_only_one_radar_scan_can_hold_the_shared_redis_lease():
    from api.repositories.signals import InMemorySignalRepository
    from api.repositories.sweeps import InMemorySweepStore
    from api.services.market_intelligence import MarketIntelligenceService, _RefreshLeaseBusy

    class SlowRadarRest:
        def __init__(self):
            self.calls = 0
            self.started = asyncio.Event()

        async def fetch_klines(self, *args, **kwargs):
            self.calls += 1
            self.started.set()
            await asyncio.Future()

    async def scenario():
        cache = InMemoryJsonCache()
        await cache.set_json(QUOTES_KEY, {
            "BTCUSDT": {
                "symbol": "BTCUSDT",
                "price": 70000,
                "volume_24h": 2_000_000_000,
            },
        }, ttl_seconds=120)
        rest = SlowRadarRest()
        first = MarketIntelligenceService(cache, rest, InMemorySignalRepository([]), InMemorySweepStore())
        second = MarketIntelligenceService(cache, rest, InMemorySignalRepository([]), InMemorySweepStore())

        active = asyncio.create_task(first._build_radar_snapshot_with_lease(force_refresh=True))
        await asyncio.wait_for(rest.started.wait(), timeout=0.5)
        await asyncio.sleep(0)
        try:
            try:
                await second._build_radar_snapshot_with_lease(force_refresh=True)
                raise AssertionError("A second instance must not start the same Radar scan")
            except _RefreshLeaseBusy:
                pass
            assert rest.calls == 4
        finally:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)

    run(scenario())
