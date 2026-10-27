from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from api.cache import InMemoryJsonCache
from api.repositories.signals import InMemorySignalRepository
from api.security import Principal, current_principal, require_analytical_access
from api.services.market import FEED_STATUS_KEY, QUOTES_KEY, MarketDataService, quote_from_ticker
from main import create_app
from worker.market_feed import MarketFeedWorker, parse_liquidations, parse_mark_prices, parse_mini_tickers


class FakeRest:
    def __init__(self, tickers=None, funding=None, klines=None, error=False):
        self.tickers = tickers or []
        self.funding = funding or []
        self.klines = klines or []
        self.error = error
        self.closed = False
        self.calls = []

    async def fetch_24h_tickers(self):
        self.calls.append("tickers")
        if self.error:
            from worker.binance_rest import BinanceDataError
            raise BinanceDataError("offline")
        return self.tickers

    async def fetch_funding(self):
        self.calls.append("funding")
        if self.error:
            from worker.binance_rest import BinanceDataError
            raise BinanceDataError("offline")
        return self.funding

    async def fetch_klines(self, symbol, interval, limit):
        self.calls.append(("klines", symbol, interval, limit))
        if self.error:
            from worker.binance_rest import BinanceDataError
            raise BinanceDataError("offline")
        return self.klines

    async def close(self):
        self.closed = True


def run(coro):
    return asyncio.run(coro)


def test_parse_mini_tickers_handles_combined_stream_and_filters_coin_margined():
    raw = {
        "stream": "!miniTicker@arr",
        "data": [
            {"e": "24hrMiniTicker", "s": "BTCUSDT", "c": "104", "o": "100", "h": "105", "l": "95", "q": "500000", "st": 1},
            {"e": "24hrMiniTicker", "s": "BTCUSD_241227", "c": "100000", "o": "99000", "st": 2},
            {"e": "24hrMiniTicker", "s": "ETHUSDT", "c": "0", "o": "1", "st": 1},
        ],
    }
    items = parse_mini_tickers(raw)
    assert len(items) == 1
    assert items[0]["s"] == "BTCUSDT"
    assert items[0]["c"] == 104
    quote = quote_from_ticker(items[0])
    assert abs(quote["change_pct"] - 4.0) < 1e-9
    assert quote["volume_24h"] == 500000


def test_liquidation_parser_keeps_original_side_semantics_and_amount():
    event = {"stream": "!forceOrder@arr", "data": {
        "e": "forceOrder", "E": 1761400000000,
        "o": {"s": "BTCUSDT", "S": "SELL", "ap": "100", "p": "99", "z": "2", "q": "3", "T": 1761400000000},
    }}
    rows = parse_liquidations(event, received_at=1_761_400_001)
    assert len(rows) == 1
    assert rows[0]["side"] == "long"
    assert rows[0]["amount_usdt"] == 200
    event["data"]["o"]["S"] = "BUY"
    assert parse_liquidations(event, received_at=1_761_400_001)[0]["side"] == "short"


def test_mark_price_parser_and_worker_cache_publish_real_funding():
    raw = {"stream": "!markPrice@arr@1s", "data": [
        {"e": "markPriceUpdate", "s": "BTCUSDT", "p": "101.1", "i": "101.0", "r": "0.00012", "T": 1761400300000, "st": 1},
        {"e": "markPriceUpdate", "s": "BTCUSD_241227", "p": "101.1", "i": "101.0", "r": "0.00012", "st": 2},
    ]}
    rows = parse_mark_prices(raw)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["lastFundingRate"] == 0.00012
    cache = InMemoryJsonCache()
    worker = MarketFeedWorker(cache, clock=time.time)
    worker.connected = True
    result = run(worker.process_message(raw))
    assert result["funding_updates"] == 1
    funding = run(cache.get_json("market:funding:v1"))
    assert funding[0]["lastFundingRate"] == 0.00012
    assert any(event[1]["event"] == "funding.update" for event in cache.published)


def test_worker_process_message_writes_quotes_liquidations_events_and_movers_status():
    cache = InMemoryJsonCache()
    now = time.time()
    worker = MarketFeedWorker(cache, clock=lambda: now)
    worker.connected = True
    tick_message = {"stream": "!miniTicker@arr", "data": [
        {"e": "24hrMiniTicker", "s": "BTCUSDT", "c": "101", "o": "100", "h": "103", "l": "97", "q": "9000", "st": 1},
    ]}
    result = run(worker.process_message(tick_message))
    assert result == {"quotes_updated": 1, "liquidations_received": 0, "funding_updates": 0}
    quotes = run(cache.get_json(QUOTES_KEY))
    assert quotes["BTCUSDT"]["price"] == 101
    assert quotes["BTCUSDT"]["sparkline"] == [101]
    history = run(cache.get_timed_list_json("market:price-history:v1:BTCUSDT", limit=5, retention_seconds=3900))
    assert history[0]["price"] == 101
    assert history[0]["volume_24h"] == 9000
    health = run(cache.get_json(FEED_STATUS_KEY))
    assert health["connected"] is True
    assert health["source"] == "websocket"
    assert run(cache.get_json("market:movers:v1:15"))["status"] == "collecting"
    coverage = run(cache.get_json("market:movers:coverage:v1"))
    assert coverage["collected_minutes"] >= 0

    event_ms = int(time.time() * 1000)
    liq_message = {"stream": "!forceOrder@arr", "data": {
        "e": "forceOrder", "E": event_ms,
        "o": {"s": "BTCUSDT", "S": "SELL", "ap": "100", "z": "3", "T": event_ms},
    }}
    result = run(worker.process_message(liq_message))
    assert result["liquidations_received"] == 1
    events = run(cache.get_timed_list_json("market:liquidations:events:v1", limit=10, retention_seconds=7200))
    assert events[0]["amount_usdt"] == 300
    liquidations_status = run(cache.get_json("market:liquidations:status:v1"))
    assert liquidations_status["last_event_epoch"] is not None
    names = [event[1]["event"] for event in cache.published]
    assert "price.update" in names and "liquidation.new" in names


def test_quotes_endpoint_reads_fresh_shared_cache_without_rest_call():
    now = time.time()
    cache = InMemoryJsonCache()
    quote = quote_from_ticker({"s": "BTCUSDT", "c": "100", "o": "99", "h": "101", "l": "98", "q": "123456"})
    run(cache.set_json(QUOTES_KEY, {"BTCUSDT": quote}, ttl_seconds=100))
    run(cache.set_json("market:funding:v1", [{"symbol": "BTCUSDT", "lastFundingRate": "0.0002"}], ttl_seconds=100))
    run(cache.set_json(FEED_STATUS_KEY, {
        "connected": True, "source": "websocket", "status": "healthy",
        "last_price_update_epoch": now, "last_price_update_at": "2026-10-25T12:00:00Z",
        "updated_at": "2026-10-25T12:00:00Z", "message": None,
    }, ttl_seconds=100))
    rest = FakeRest(error=True)
    app = create_app(InMemorySignalRepository([]), market_cache=cache, market_rest_client=rest)
    principal = Principal("u1", {"sub": "u1"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as client:
        response = client.get("/market/quotes")
    assert response.status_code == 200
    assert response.headers["x-ftrade-market-source"] == "websocket"
    assert response.json()[0]["symbol"] == "BTCUSDT"
    assert response.json()[0]["funding_rate"] == 0.0002
    assert rest.calls == []
    assert rest.closed is True


def test_quotes_fallback_uses_real_rest_values_and_reports_source():
    cache = InMemoryJsonCache()
    rest = FakeRest(
        tickers=[{"symbol": "BTCUSDT", "lastPrice": "100", "priceChangePercent": "2.5", "highPrice": "103", "lowPrice": "97", "quoteVolume": "500000"}],
        funding=[{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}],
    )
    app = create_app(InMemorySignalRepository([]), market_cache=cache, market_rest_client=rest)
    principal = Principal("u1", {"sub": "u1"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as client:
        response = client.get("/market/quotes")
        second = client.get("/market/quotes")
    assert response.status_code == 200
    assert second.status_code == 200
    assert response.headers["x-ftrade-market-source"] == "rest"
    btc = response.json()[0]
    assert btc["price"] == 100
    assert btc["change_pct"] == 2.5
    assert btc["volume_24h"] == 500000
    assert btc["funding_rate"] == 0.0001
    # Unsupported analytics are explicitly unknown, not filled with mock numbers.
    assert btc["open_interest"] is None
    assert btc["score"] is None
    assert "tickers" in rest.calls and "funding" in rest.calls
    assert rest.calls.count("tickers") == 1  # Fresh REST result is briefly cached, not refetched per visitor.


def test_candles_endpoint_cvd_timezone_and_contract_shape():
    start = 1761400000000
    klines = [
        [start, "100", "103", "99", "102", "12", start + 59999, "1000", 5, "6", "700", "0"],
        [start + 60000, "102", "105", "101", "104", "10", start + 119999, "1200", 7, "4", "500", "0"],
    ]
    cache = InMemoryJsonCache()
    rest = FakeRest(klines=klines)
    app = create_app(InMemorySignalRepository([]), market_cache=cache, market_rest_client=rest)
    principal = Principal("u1", {"sub": "u1", "tz": "UTC"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as client:
        response = client.get("/market/candles?symbol=BTCUSDT&timeframe=1m&tz=UTC")
        five_minute = client.get("/market/candles?symbol=BTCUSDT&timeframe=5m&tz=UTC")
        invalid_tf = client.get("/market/candles?symbol=BTCUSDT&timeframe=1w")
        valid = client.get("/market/candles?symbol=BTCUSDT&timeframe=1h&tz=UTC")
    assert response.status_code == 200
    assert five_minute.status_code == 200
    assert invalid_tf.status_code == 422
    assert valid.status_code == 200
    assert ("klines", "BTCUSDT", "1m", 120) in rest.calls
    assert ("klines", "BTCUSDT", "5m", 120) in rest.calls
    assert ("klines", "BTCUSDT", "1h", 120) in rest.calls
    data = valid.json()
    assert len(data) == 2
    assert [c["timestamp"] for c in data] == sorted(c["timestamp"] for c in data)
    assert data[0]["volume"] == 1000
    assert data[0]["cvd"] == 400  # 2 * 700 - 1000
    assert data[1]["cvd"] == 200  # prior CVD + (2 * 500 - 1200)
    assert set(("time", "timestamp", "open", "high", "low", "close", "volume", "cvd")).issubset(data[0])


def test_feed_unavailable_returns_503_without_fabricating_quotes():
    cache = InMemoryJsonCache()
    rest = FakeRest(error=True)
    app = create_app(InMemorySignalRepository([]), market_cache=cache, market_rest_client=rest)
    principal = Principal("u1", {"sub": "u1"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as client:
        response = client.get("/market/quotes")
    assert response.status_code == 503
    assert response.json()["code"] == "DATA_UNAVAILABLE"


def test_stale_cache_is_used_when_rest_fails_and_is_marked_degraded():
    cache = InMemoryJsonCache()
    quote = quote_from_ticker({"s": "ETHUSDT", "c": "2500", "o": "2400", "h": "2600", "l": "2300", "q": "456"})
    run(cache.set_json(QUOTES_KEY, {"ETHUSDT": quote}, ttl_seconds=7200))
    run(cache.set_json(FEED_STATUS_KEY, {
        "connected": False, "source": "websocket", "status": "degraded",
        "last_price_update_epoch": time.time() - 120, "updated_at": "2026-10-25T12:00:00Z",
        "last_price_update_at": "2026-10-25T12:00:00Z", "message": "feed stale",
    }, ttl_seconds=7200))
    app = create_app(InMemorySignalRepository([]), market_cache=cache, market_rest_client=FakeRest(error=True))
    principal = Principal("u1", {"sub": "u1"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as client:
        response = client.get("/market/quotes")
    assert response.status_code == 200
    assert response.headers["x-ftrade-market-status"] == "degraded"
    assert response.headers["x-ftrade-market-source"] == "cache"
    assert response.json()[0]["symbol"] == "ETHUSDT"


def test_live_feed_does_not_read_redis_history_for_every_ticker_frame():
    class CountingCache(InMemoryJsonCache):
        def __init__(self):
            super().__init__()
            self.history_reads = 0
            self.history_batches = []

        async def get_timed_list_json(self, key, *, limit=100, retention_seconds=7200):
            self.history_reads += 1
            return await super().get_timed_list_json(key, limit=limit, retention_seconds=retention_seconds)

        async def append_timed_json_many(self, entries):
            self.history_batches.append(len(entries))
            return await super().append_timed_json_many(entries)

    clock = [time.time()]
    cache = CountingCache()
    worker = MarketFeedWorker(cache, clock=lambda: clock[0])
    worker.connected = True

    def tick(price):
        return {"stream": "!miniTicker@arr", "data": [
            {"e": "24hrMiniTicker", "s": "BTCUSDT", "c": str(price), "o": "100", "h": str(price), "l": "99", "q": "900000", "st": 1},
            {"e": "24hrMiniTicker", "s": "ETHUSDT", "c": str(price * 10), "o": "1000", "h": str(price * 10), "l": "990", "q": "500000", "st": 1},
        ]}

    run(worker.process_message(tick(101)))
    assert cache.history_batches == [2]
    assert cache.history_reads == 0

    # A fresh market frame one second later updates prices but does not issue a
    # Redis ZSET history read or write for either symbol.
    clock[0] += 1
    run(worker.process_message(tick(102)))
    assert cache.history_batches == [2]
    assert cache.history_reads == 0
    quotes = run(cache.get_json(QUOTES_KEY))
    assert quotes["BTCUSDT"]["price"] == 102


def test_quote_health_uses_price_tick_age_not_only_auxiliary_websocket_messages():
    now = time.time()
    cache = InMemoryJsonCache()
    run(cache.set_json(FEED_STATUS_KEY, {
        "connected": True,
        "source": "websocket",
        "status": "healthy",
        "last_message_epoch": now,
        "last_price_update_epoch": now - 20,
        "last_message_at": "2026-10-26T00:55:00Z",
        "last_price_update_at": "2026-10-26T00:54:40Z",
    }, ttl_seconds=100))
    service = MarketDataService(cache, FakeRest(), clock=lambda: now)
    health = run(service.feed_status())
    assert health.status == "degraded"
    assert health.source == "cache"
    assert health.stale_seconds == 20


def test_combined_stream_url_uses_futures_root_stream_endpoint():
    from worker.market_feed import STREAM_URL

    assert STREAM_URL.startswith("wss://fstream.binance.com/stream?streams=") or "/stream?streams=" in STREAM_URL
    assert "/market/stream?streams=" not in STREAM_URL
