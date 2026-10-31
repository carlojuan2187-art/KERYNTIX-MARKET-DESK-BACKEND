import asyncio

import httpx
import pytest

from worker.binance_rest import BinanceDataError, BinanceRestClient


def test_top_trader_endpoints_are_not_supported_by_public_analytics_client():
    async def scenario():
        async with httpx.AsyncClient(base_url="https://fapi.binance.com") as http:
            client = BinanceRestClient(client=http, retries=1)
            for path in (
                "/futures/data/topLongShortAccountRatio",
                "/futures/data/topLongShortPositionRatio",
            ):
                with pytest.raises(ValueError, match="UNSUPPORTED_ANALYTICS_ENDPOINT"):
                    await client.fetch_endpoint_json(path, {"symbol": "BTCUSDT", "period": "1h", "limit": 1})

    asyncio.run(scenario())


def test_public_analytics_client_allows_usdm_funding_history_endpoint():
    async def scenario():
        requested = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request)
            return httpx.Response(200, json=[{
                "symbol": "BTCUSDT", "fundingRate": "0.0001",
                "fundingTime": 1_800_000_000_000, "markPrice": "68000.0",
            }])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="https://fapi.binance.com", transport=transport) as http:
            client = BinanceRestClient(client=http, retries=1)
            rows = await client.fetch_endpoint_json(
                "/fapi/v1/fundingRate", {"symbol": "BTCUSDT", "limit": 25}
            )

        assert len(rows) == 1
        assert rows[0]["fundingRate"] == "0.0001"
        assert len(requested) == 1
        assert requested[0].url.path == "/fapi/v1/fundingRate"
        assert requested[0].url.params["symbol"] == "BTCUSDT"
        assert requested[0].url.params["limit"] == "25"

    asyncio.run(scenario())


def test_binance_rate_limit_retry_honors_retry_after_header():
    async def scenario():
        requests = 0
        delays = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal requests
            requests += 1
            if requests == 1:
                return httpx.Response(429, headers={"Retry-After": "2"}, json={"code": -1003})
            return httpx.Response(200, json=[])

        async def fake_sleep(delay: float):
            delays.append(delay)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url="https://fapi.binance.com", transport=transport) as http:
            client = BinanceRestClient(client=http, retries=2, sleep=fake_sleep)
            result = await client.fetch_24h_tickers()
        assert result == []
        assert requests == 2
        assert delays == [2.0]

    asyncio.run(scenario())
