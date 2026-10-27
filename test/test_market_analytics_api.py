from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.cache import InMemoryJsonCache
from api.repositories.signals import InMemorySignalRepository
from api.security import Principal, current_principal, require_analytical_access
from api.services.analytics import MarketAnalyticsService
from engine import crypto_calendar
from main import create_app


class FakeAnalyticsRest:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.api_key_configured = True
        self.fail_all_analytics = False
        self.short_oi_history = False
        self.failed_paths = set()
        self.tickers = [
            {"symbol": f"COIN{i}USDT", "quoteVolume": str(100_000_000 - i * 1_000_000)}
            for i in range(1, 13)
        ]
        self.tickers.append({"symbol": "BTCUSDT", "quoteVolume": "2_000_000_000"})

    def _klines(self, interval: str, limit: int) -> list[list[object]]:
        step = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "5m": 300_000}[interval]
        end = int(datetime.now(timezone.utc).timestamp() * 1000)
        rows = []
        # Deterministic rising series with finite OHLCV and real Binance row positions.
        for i in range(limit):
            close = 100 + i * 0.3
            open_price = close - 0.1
            start = end - (limit - i) * step
            rows.append([
                start, str(open_price), str(close + 0.4), str(open_price - 0.4),
                str(close), "1000", start + step - 1, str(close * 1000), "100",
                "650", str(close * 650), "0",
            ])
        return rows

    async def fetch_klines(self, symbol, interval, limit, **kwargs):
        self.calls.append(("klines", symbol, interval, limit))
        if self.fail_all_analytics:
            from worker.binance_rest import BinanceDataError
            raise BinanceDataError("offline")
        return self._klines(interval, limit)

    async def fetch_24h_tickers(self):
        self.calls.append(("tickers",))
        return self.tickers

    async def fetch_funding(self):
        self.calls.append(("funding",))
        return [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}]

    async def fetch_endpoint_json(self, path, params=None):
        self.calls.append(("endpoint", path, params))
        if self.fail_all_analytics or path in self.failed_paths:
            raise RuntimeError("offline")
        symbol = (params or {}).get("symbol", "BTCUSDT")
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        if path.endswith("premiumIndex"):
            return {"symbol": symbol, "lastFundingRate": "0.0001", "nextFundingTime": 1_800_000_000_000,
                    "time": now_ms, "markPrice": "101.2", "indexPrice": "101.0"}
        if path.endswith("/openInterest"):
            return {"symbol": symbol, "openInterest": "110", "time": now_ms}
        if path.endswith("openInterestHist"):
            values = [99.0] + [100.0] * 23 + [105.0, 110.0]
            if self.short_oi_history:
                values = values[-4:]
                start_offset = len(values) - 1
                return [{"sumOpenInterestValue": str(v), "sumOpenInterest": str(v / 101.2),
                         "timestamp": now_ms - (start_offset - i) * 3_600_000} for i, v in enumerate(values)]
            return [{"sumOpenInterestValue": str(v), "sumOpenInterest": str(v / 101.2),
                     "timestamp": now_ms - (25 - i) * 3_600_000} for i, v in enumerate(values)]
        if path.endswith("globalLongShortAccountRatio"):
            rows = []
            for i in range(26):
                long_account = "0.64" if i < 2 else "0.58"
                short_account = "0.36" if i < 2 else "0.42"
                ratio = "1.20" if i < 2 else "1.40"
                rows.append({"longShortRatio": ratio, "longAccount": long_account, "shortAccount": short_account,
                             "timestamp": now_ms - (25 - i) * 3_600_000})
            return rows
        if path.endswith("fundingRate"):
            return [{"symbol": symbol, "fundingRate": "0.0001", "fundingTime": now_ms - (24 - i) * 8 * 3_600_000,
                     "markPrice": "101.2"} for i in range(25)]
        if path.endswith("/basis"):
            return [{"pair": symbol, "contractType": "PERPETUAL", "basisRate": "0.0004",
                     "annualizedBasisRate": "0.12", "basis": "0.04", "futuresPrice": "101.04",
                     "indexPrice": "101.0", "timestamp": now_ms - (24 - i) * 3_600_000}
                    for i in range(25)]
        if path.endswith("takerlongshortRatio"):
            return [{"buySellRatio": "1.2", "buyVol": "120", "sellVol": "100",
                     "timestamp": now_ms - (15 - i) * 15 * 60_000} for i in range(16)]
        if path.endswith("ticker/24hr"):
            return {"symbol": symbol, "priceChangePercent": "2.5", "closeTime": now_ms}
        if path.endswith("topLongShortAccountRatio"):
            return [{"longShortRatio": "1.20", "longAccount": "0.55", "shortAccount": "0.45",
                     "timestamp": now_ms - 3_600_000} for _ in range(25)]
        if path.endswith("topLongShortPositionRatio"):
            return [{"longShortRatio": "1.10", "longAccount": "0.53", "shortAccount": "0.47",
                     "timestamp": now_ms - 3_600_000} for _ in range(25)]
        if path.endswith("/depth"):
            bids = [[str(100 - i), str(2 + i)] for i in range(20)]
            asks = [[str(101 + i), str(1 + i)] for i in range(20)]
            return {"lastUpdateId": 98765, "bids": bids, "asks": asks}
        raise AssertionError(path)

    async def close(self):
        self.closed = True


def run(coro):
    return asyncio.run(coro)


def get_test_app(rest=None):
    cache = InMemoryJsonCache()
    rest = rest or FakeAnalyticsRest()
    app = create_app(InMemorySignalRepository([]), market_cache=cache, market_rest_client=rest)
    principal = Principal("test-user", {"sub": "test-user", "tz": "America/Puerto_Rico"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    return app, cache, rest


def test_derivatives_api_preserves_partial_metrics_and_caches_response():
    app, cache, rest = get_test_app()
    with TestClient(app) as client:
        response = client.get("/derivatives?symbol=BTCUSDT")
        calls_after_first = len(rest.calls)
        again = client.get("/derivatives?symbol=BTCUSDT")
    assert response.status_code == 200
    data = response.json()
    assert data["funding_rate"] == pytest.approx(0.0001)
    assert data["mark_index_delta"] == pytest.approx((101.2 / 101.0 - 1) * 100)
    assert data["open_interest"] == pytest.approx(110 * 101.2)
    assert data["open_interest_contracts"] == 110
    assert data["oi_change_1h"] == pytest.approx((110 / 105 - 1) * 100)
    assert data["oi_change_24h"] == pytest.approx(10)
    assert data["long_short_ratio"]["percent_long"] == pytest.approx(58)
    assert data["long_short_ratio"]["ratio_24h_ago"] == pytest.approx(1.2)
    assert data["long_short_ratio"]["percent_long_24h_ago"] == pytest.approx(64)
    assert data["top_trader_accounts"]["long"] == pytest.approx(55)
    assert data["top_trader_positions"]["long"] == pytest.approx(53)
    assert len(data["oi_history"]) == 26
    assert len(data["funding_history"]) == 25
    assert len(data["basis_history"]) == 25
    assert data["basis_rate_pct"] == pytest.approx(0.04)
    assert data["annualized_basis_pct"] == pytest.approx(12.0)
    assert data["sources"]["basis"]["status"] == "available"
    assert {row["label"] for row in data["taker_flow"]["windows"]} == {"15m", "1h", "4h"}
    assert data["sources"]["open_interest_history"]["status"] == "available"
    assert again.status_code == 200
    assert len(rest.calls) == calls_after_first
    assert response.headers["x-ftrade-analytics-source"] == "binance-futures"


def test_derivatives_api_does_not_fabricate_failed_metrics():
    rest = FakeAnalyticsRest()
    rest.failed_paths = {"/fapi/v1/openInterest", "/futures/data/openInterestHist", "/futures/data/globalLongShortAccountRatio", "/futures/data/basis", "/futures/data/topLongShortPositionRatio", "/futures/data/topLongShortAccountRatio"}
    app, _, _ = get_test_app(rest)
    with TestClient(app) as client:
        response = client.get("/derivatives?symbol=ETHUSDT")
    assert response.status_code == 200
    data = response.json()
    assert data["funding_rate"] == pytest.approx(0.0001)
    assert data["open_interest"] is None
    assert data["annualized_basis_pct"] is None
    assert data["sources"]["basis"]["status"] == "unavailable"
    assert data["oi_change_1h"] is None
    assert data["oi_change_24h"] is None
    assert data["long_short_ratio"]["percent_long"] is None
    assert data["top_trader_ratio"]["long"] is None
    assert data["sources"]["top_trader_accounts"]["status"] == "unavailable"


def test_derivatives_does_not_label_short_oi_history_as_24h_change():
    rest = FakeAnalyticsRest()
    rest.short_oi_history = True
    app, _, _ = get_test_app(rest)
    with TestClient(app) as client:
        response = client.get("/derivatives?symbol=BTCUSDT")
    assert response.status_code == 200
    data = response.json()
    assert data["oi_change_24h"] is None
    assert data["oi_history_coverage_hours"] < 23
    assert data["sources"]["open_interest_history"]["status"] == "partial"


def test_derivatives_marks_top_traders_as_config_missing_without_key():
    rest = FakeAnalyticsRest()
    rest.api_key_configured = False
    app, _, _ = get_test_app(rest)
    with TestClient(app) as client:
        response = client.get("/derivatives?symbol=BTCUSDT")
    assert response.status_code == 200
    data = response.json()
    assert data["top_trader_accounts"]["long"] is None
    assert data["top_trader_positions"]["long"] is None
    assert data["sources"]["top_trader_accounts"]["status"] == "api_key_missing"
    assert data["sources"]["top_trader_positions"]["status"] == "api_key_missing"
    assert not any("topLongShort" in call[1] for call in rest.calls if call[0] == "endpoint")


def test_derivatives_rejects_bad_symbol():
    app, _, _ = get_test_app()
    with TestClient(app) as client:
        response = client.get("/derivatives?symbol=BTC/USD")
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_SYMBOL"


def test_flow_api_uses_exchange_klines_and_returns_real_ohlcv_with_cvd():
    app, _, rest = get_test_app()
    with TestClient(app) as client:
        response = client.get("/market/flow?symbol=BTCUSDT")
        cached = client.get("/market/flow?symbol=BTCUSDT")
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "binance_futures_klines_5m"
    assert [row["label"] for row in data["windows"]] == ["15m", "1h", "4h"]
    assert 0 <= data["windows"][0]["buy_pct"] <= 100
    assert len(data["cvd"]) == 48
    assert {"time", "timestamp", "open", "high", "low", "close", "volume", "cvd"}.issubset(data["cvd"][0])
    assert data["quality"]["closed_candles"] >= 192
    assert data["quality"]["coverage_complete"] is True
    assert data["quality"]["volume_ratio_4h_ready"] is True
    assert data["price_cvd"]["status"] == "buyers_confirmed"
    assert cached.status_code == 200
    assert sum(1 for call in rest.calls if call[:3] == ("klines", "BTCUSDT", "5m")) == 1



def test_order_book_api_returns_real_depth_metrics_and_uses_short_cache():
    app, _, rest = get_test_app()
    with TestClient(app) as client:
        response = client.get("/market/orderbook?symbol=BTCUSDT&limit=20")
        count_after_first = sum(1 for call in rest.calls if call[:2] == ("endpoint", "/fapi/v1/depth"))
        cached = client.get("/market/orderbook?symbol=BTCUSDT&limit=20")
        invalid_limit = client.get("/market/orderbook?symbol=BTCUSDT&limit=30")
        invalid_symbol = client.get("/market/orderbook?symbol=BTC/USD&limit=20")
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "binance-futures-order-book"
    assert data["best_bid"] == pytest.approx(100)
    assert data["best_ask"] == pytest.approx(101)
    assert data["spread"] == pytest.approx(1)
    assert data["spread_pct"] > 0
    assert len(data["bids"]) == 20 and len(data["asks"]) == 20
    assert data["bid_depth_usdt"] > 0 and data["ask_depth_usdt"] > 0
    assert cached.status_code == 200
    assert sum(1 for call in rest.calls if call[:2] == ("endpoint", "/fapi/v1/depth")) == count_after_first == 1
    assert invalid_limit.status_code == 422
    assert invalid_symbol.status_code == 422

def test_calendar_api_returns_events_sorted_and_exposes_bls_health(monkeypatch):
    monkeypatch.setattr(crypto_calendar, "get_bls_events", lambda: {"events": [], "ok": True})
    app, _, _ = get_test_app()
    with TestClient(app) as client:
        response = client.get("/calendar?view=7")
    assert response.status_code == 200
    events = response.json()
    assert events
    dates = [event["date"] for event in events]
    assert dates == sorted(dates)
    assert response.headers["x-ftrade-calendar-bls"] == "available"
    assert all({"id", "title", "date", "impact", "category", "countdown"}.issubset(event) for event in events)


def test_calendar_rejects_unsupported_window():
    app, _, _ = get_test_app()
    with TestClient(app) as client:
        response = client.get("/calendar?view=14")
    assert response.status_code == 422


def test_regime_api_maps_original_bot_algorithm_to_frontend_contract():
    app, cache, rest = get_test_app()
    # Avoid many breadth calls for this unit: the service's algorithm still computes the BTC regime.
    run(cache.set_json("market:quotes:v1", {"BTCUSDT": {"symbol": "BTCUSDT", "volume_24h": 2_000_000_000}}, ttl_seconds=300))
    with TestClient(app) as client:
        response = client.get("/market/regime")
        second = client.get("/market/regime")
    assert response.status_code == 200
    data = response.json()
    assert data["trend"] in {"TENDENCIA ALCISTA", "TENDENCIA BAJISTA", "RANGO", "TRANSICIÓN", "ALTA VOLATILIDAD"}
    assert isinstance(data["adx"], (int, float))
    assert isinstance(data["atr_pct"], (int, float))
    assert data["timeframes"]["1h"]["regime"]
    assert second.json()["updated_at"] == data["updated_at"]


def test_regime_returns_503_when_source_has_no_1h_data():
    rest = FakeAnalyticsRest()
    rest.fail_all_analytics = True
    app, cache, _ = get_test_app(rest)
    run(cache.set_json("market:quotes:v1", {"BTCUSDT": {"symbol": "BTCUSDT", "volume_24h": 2_000_000_000}}, ttl_seconds=300))
    with TestClient(app) as client:
        response = client.get("/market/regime")
    assert response.status_code == 503
    assert response.json()["code"] == "REGIME_UNAVAILABLE"
