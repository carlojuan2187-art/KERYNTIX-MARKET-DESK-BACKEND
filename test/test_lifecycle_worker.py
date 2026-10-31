from datetime import datetime, timedelta, timezone

import pytest

from api.cache import InMemoryJsonCache
from api.repositories.lifecycle import InMemoryLifecycleRepository
from api.services.market import FEED_STATUS_KEY, QUOTES_KEY
from worker.signal_lifecycle import SignalLifecycleWorker


def utc_now():
    return datetime.now(timezone.utc)


def pending_signal(now):
    created = now - timedelta(minutes=2)
    expires = now + timedelta(minutes=13)
    return {
        "_id": "mongo-pending", "id": "pending-uuid", "symbol": "BTCUSDT", "direction": "LONG",
        "visibility": "premium", "entry_price": 100.5, "entry_zone": {"low": 100.0, "high": 101.0},
        "stop_loss": 98.0, "take_profits": [110.0, 115.0], "created_at": created,
        "entry_expires_at": expires, "status": "PENDING",
        "lifecycle": {"status": "PENDING", "created_at": created, "entry_expires_at": expires},
        "outcome": {},
    }


def touched_signal(now):
    touched = now - timedelta(minutes=4)
    return {
        "_id": "mongo-open", "id": "open-uuid", "symbol": "BTCUSDT", "direction": "LONG",
        "visibility": "premium", "entry_price": 100.5, "entry_zone": {"low": 100.0, "high": 101.0},
        "stop_loss": 98.0, "take_profits": [103.0, 105.0], "created_at": touched - timedelta(minutes=1),
        "status": "TOUCHED", "touched_at": touched,
        "lifecycle": {"status": "TOUCHED", "created_at": touched - timedelta(minutes=1), "touched_at": touched},
        "outcome": {"state": "OPEN", "tracked_until": touched, "tp1_at": None, "recipient_ids": []},
    }


def kline(opened: datetime, *, high: float, low: float, close: float = 104.0):
    open_ms = int(opened.replace(tzinfo=timezone.utc).timestamp() * 1000)
    close_ms = open_ms + 59_999
    return [open_ms, "100.0", str(high), str(low), str(close), "1000", close_ms, "100000", 10, "500", "50000", "0"]


class FakeMarket:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.klines_calls = []

    async def fetch_24h_tickers(self):
        return [{"symbol": "BTCUSDT", "lastPrice": "100.5"}]

    async def fetch_klines(self, symbol, interval, limit, *, start_time_ms=None, end_time_ms=None):
        self.klines_calls.append((symbol, interval, limit, start_time_ms, end_time_ms))
        return list(self.rows)


@pytest.mark.asyncio
async def test_pending_signal_moves_to_touched_and_publishes_event():
    now = utc_now()
    cache = InMemoryJsonCache()
    await cache.set_json(QUOTES_KEY, {"BTCUSDT": {"symbol": "BTCUSDT", "price": 100.5}})
    await cache.set_json(FEED_STATUS_KEY, {"last_price_update_epoch": now.timestamp(), "connected": True})
    repository = InMemoryLifecycleRepository([pending_signal(now)])
    worker = SignalLifecycleWorker(FakeMarket(), repository, cache, clock=lambda: now)
    changed = await worker.process_pending_once(now=now)
    assert changed == 1
    saved = repository.snapshot_signals()[0]
    assert saved["status"] == "TOUCHED"
    assert saved["lifecycle"]["status"] == "TOUCHED"
    assert saved["outcome"]["state"] == "OPEN"
    assert saved["touch_price"] == 100.5
    assert cache.published[0][0] == "signals:events:v1"
    assert cache.published[0][1]["type"] == "signal.status.updated"


@pytest.mark.asyncio
async def test_open_trade_closes_tp2_persists_immutable_result_and_r():
    now = utc_now()
    signal = touched_signal(now)
    candle_open = signal["outcome"]["tracked_until"] + timedelta(minutes=1)
    market = FakeMarket([kline(candle_open, high=105.5, low=103.1, close=105.2)])
    cache = InMemoryJsonCache()
    repository = InMemoryLifecycleRepository([signal])
    worker = SignalLifecycleWorker(market, repository, cache, clock=lambda: now, outcome_check_seconds=5)
    changed = await worker.track_open_trades_once(now=now, force=True)
    assert changed == 1
    saved = repository.snapshot_signals()[0]
    assert saved["outcome"]["state"] == "CLOSED"
    assert saved["outcome"]["result"] == "TP2"
    assert saved["operation_result"] == "TP2"
    assert saved["r_result"] > 0
    results = repository.list_results()
    assert len(results) == 1
    assert results[0]["signal_id"] == "open-uuid"
    assert results[0]["result"] == "TP2"
    assert cache.published and cache.published[-1][1]["type"] == "signal.outcome.updated"


@pytest.mark.asyncio
async def test_repeat_outcome_processing_is_idempotent_after_close():
    now = utc_now()
    signal = touched_signal(now)
    candle_open = signal["outcome"]["tracked_until"] + timedelta(minutes=1)
    market = FakeMarket([kline(candle_open, high=105.5, low=103.1, close=105.2)])
    cache = InMemoryJsonCache()
    repository = InMemoryLifecycleRepository([signal])
    worker = SignalLifecycleWorker(market, repository, cache, clock=lambda: now, outcome_check_seconds=5)
    assert await worker.track_open_trades_once(now=now, force=True) == 1
    assert await worker.track_open_trades_once(now=now + timedelta(minutes=1), force=True) == 0
    assert len(repository.list_results()) == 1


@pytest.mark.asyncio
async def test_open_trades_reuse_one_candle_fetch_per_symbol():
    now = utc_now()
    base = touched_signal(now)
    signals = []
    for index in range(3):
        row = dict(base)
        row["_id"] = f"mongo-open-{index}"
        row["id"] = f"open-uuid-{index}"
        row["outcome"] = dict(base["outcome"])
        signals.append(row)
    candle_open = base["outcome"]["tracked_until"] + timedelta(minutes=1)
    market = FakeMarket([kline(candle_open, high=105.5, low=103.1, close=105.2)])
    cache = InMemoryJsonCache()
    repository = InMemoryLifecycleRepository(signals)
    worker = SignalLifecycleWorker(market, repository, cache, clock=lambda: now, outcome_check_seconds=5)

    changed = await worker.track_open_trades_once(now=now, force=True)

    assert changed == 3
    assert len(market.klines_calls) == 1
    assert market.klines_calls[0][0] == "BTCUSDT"
    assert all(row["outcome"]["state"] == "CLOSED" for row in repository.snapshot_signals())
    assert len(repository.list_results()) == 3


@pytest.mark.asyncio
async def test_shared_lifecycle_history_chunks_old_ranges_so_recent_signals_are_not_starved(monkeypatch):
    import worker.signal_lifecycle as lifecycle_module

    now = utc_now()
    old = touched_signal(now)
    old["_id"] = "mongo-old"
    old["id"] = "open-old"
    old["outcome"] = {"state": "OPEN", "tracked_until": now - timedelta(days=2), "tp1_at": None, "recipient_ids": []}
    recent = touched_signal(now)
    recent["_id"] = "mongo-recent"
    recent["id"] = "open-recent"
    recent["outcome"] = {"state": "OPEN", "tracked_until": now - timedelta(minutes=4), "tp1_at": None, "recipient_ids": []}

    repository = InMemoryLifecycleRepository([old, recent])
    worker = SignalLifecycleWorker(FakeMarket(), repository, InMemoryJsonCache(), clock=lambda: now)
    monkeypatch.setattr(lifecycle_module, "OUTCOME_MAX_PAGES", 1)
    ranges = []

    async def fetch_range(symbol, start_at, end_at):
        ranges.append((symbol, start_at, end_at))
        return []

    monkeypatch.setattr(worker, "_fetch_candles_since", fetch_range)
    assert await worker.track_open_trades_once(now=now, force=True) == 0
    assert len(ranges) > 1
    assert ranges[-1][2] == now.replace(tzinfo=None)
    assert ranges[-1][1] <= now.replace(tzinfo=None) - timedelta(minutes=4)


@pytest.mark.asyncio
async def test_open_trade_uses_fresh_websocket_prices_for_tp1_protection_and_close():
    now = utc_now()
    signal = touched_signal(now)
    cache = InMemoryJsonCache()
    repository = InMemoryLifecycleRepository([signal])
    market = FakeMarket([])
    worker = SignalLifecycleWorker(
        market, repository, cache, clock=lambda: now,
        outcome_check_seconds=20,
    )

    first_at = now.timestamp()
    await cache.set_json(QUOTES_KEY, {
        "BTCUSDT": {
            "symbol": "BTCUSDT", "price": 103.1,
            "price_updated_epoch": first_at,
        }
    })
    await cache.set_json(FEED_STATUS_KEY, {
        "source": "websocket", "connected": True,
        "last_price_update_epoch": first_at,
    })

    changed = await worker.track_open_trades_once(now=now)
    assert changed == 1
    saved = repository.snapshot_signals()[0]
    assert saved["outcome"]["state"] == "TP1_HIT"
    assert saved["outcome"]["protective_stop_price"] == 103.0
    assert saved["outcome"]["last_live_price_at"] is not None
    assert repository.list_results() == []

    second_now = now + timedelta(seconds=2)
    second_at = second_now.timestamp()
    await cache.set_json(QUOTES_KEY, {
        "BTCUSDT": {
            "symbol": "BTCUSDT", "price": 102.99,
            "price_updated_epoch": second_at,
        }
    })
    await cache.set_json(FEED_STATUS_KEY, {
        "source": "websocket", "connected": True,
        "last_price_update_epoch": second_at,
    })

    changed = await worker.track_open_trades_once(now=second_now)
    assert changed == 1
    saved = repository.snapshot_signals()[0]
    assert saved["outcome"]["state"] == "CLOSED"
    assert saved["outcome"]["result"] == "TP1"
    assert saved["outcome"]["exit_price"] == 103.0
    assert saved["outcome"]["exit_via"] == "TP1_PROTECTIVE_STOP_LIVE_PRICE"
    assert len(repository.list_results()) == 1
    assert repository.list_results()[0]["r_real"] == 1.0


@pytest.mark.asyncio
async def test_stale_or_rest_only_quote_never_moves_protective_stop():
    now = utc_now()
    signal = touched_signal(now)
    cache = InMemoryJsonCache()
    repository = InMemoryLifecycleRepository([signal])
    worker = SignalLifecycleWorker(FakeMarket([]), repository, cache, clock=lambda: now)
    epoch = now.timestamp()

    await cache.set_json(QUOTES_KEY, {
        "BTCUSDT": {"symbol": "BTCUSDT", "price": 103.5, "price_updated_epoch": epoch}
    })
    await cache.set_json(FEED_STATUS_KEY, {
        "source": "rest", "connected": True, "last_price_update_epoch": epoch,
    })
    assert await worker.track_open_trades_once(now=now) == 0
    assert repository.snapshot_signals()[0]["outcome"]["state"] == "OPEN"


@pytest.mark.asyncio
async def test_live_tp2_snapshot_cannot_override_an_older_unprocessed_stop_loss():
    now = utc_now()
    signal = touched_signal(now)
    # This closed candle happened after the last tracked cursor but before the
    # latest live quote. Its SL must be replayed before the newer TP2 snapshot.
    candle_open = signal["outcome"]["tracked_until"] + timedelta(minutes=1)
    market = FakeMarket([kline(candle_open, high=102.0, low=97.5, close=101.0)])
    cache = InMemoryJsonCache()
    repository = InMemoryLifecycleRepository([signal])
    worker = SignalLifecycleWorker(market, repository, cache, clock=lambda: now)
    epoch = now.timestamp()
    await cache.set_json(QUOTES_KEY, {
        "BTCUSDT": {"symbol": "BTCUSDT", "price": 105.5, "price_updated_epoch": epoch}
    })
    await cache.set_json(FEED_STATUS_KEY, {
        "source": "websocket", "connected": True, "last_price_update_epoch": epoch,
    })

    changed = await worker.track_open_trades_once(now=now)

    assert changed == 1
    saved = repository.snapshot_signals()[0]
    assert saved["outcome"]["state"] == "CLOSED"
    assert saved["outcome"]["result"] == "SL"
    assert saved["outcome"]["r_real"] == -1.0
    assert saved["outcome"]["exit_via"] == "SL"
    assert repository.list_results()[0]["result"] == "SL"


@pytest.mark.asyncio
async def test_live_transition_replays_previous_tp1_before_current_market_snapshot():
    now = utc_now()
    signal = touched_signal(now)
    candle_open = signal["outcome"]["tracked_until"] + timedelta(minutes=1)
    market = FakeMarket([kline(candle_open, high=103.2, low=100.3, close=103.1)])
    cache = InMemoryJsonCache()
    repository = InMemoryLifecycleRepository([signal])
    worker = SignalLifecycleWorker(market, repository, cache, clock=lambda: now)
    epoch = now.timestamp()
    await cache.set_json(QUOTES_KEY, {
        "BTCUSDT": {"symbol": "BTCUSDT", "price": 103.1, "price_updated_epoch": epoch}
    })
    await cache.set_json(FEED_STATUS_KEY, {
        "source": "websocket", "connected": True, "last_price_update_epoch": epoch,
    })

    changed = await worker.track_open_trades_once(now=now)

    assert changed == 1
    saved = repository.snapshot_signals()[0]
    assert saved["outcome"]["state"] == "TP1_HIT"
    assert "result" not in saved["outcome"]
    assert saved["outcome"]["protective_stop_price"] == 103.0
    assert saved["outcome"]["tracked_until"] is not None
    assert saved["outcome"]["protection_armed_at"] is not None
