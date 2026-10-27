import asyncio

import httpx
import pytest

from worker.binance_rest import BinanceDataError, BinanceRestClient


def test_top_trader_market_data_sends_server_side_api_key_header():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "longShortRatio": "1.2"}])

    async def scenario():
        async with httpx.AsyncClient(
            base_url="https://fapi.binance.com",
            transport=httpx.MockTransport(handler),
        ) as http:
            client = BinanceRestClient(client=http, api_key="read-only-market-data-key", retries=1)
            response = await client.fetch_endpoint_json(
                "/futures/data/topLongShortPositionRatio",
                {"symbol": "BTCUSDT", "period": "1h", "limit": 1},
            )
            assert response[0]["longShortRatio"] == "1.2"
            assert seen[0].headers["x-mbx-apikey"] == "read-only-market-data-key"
            assert "apikey" not in str(seen[0].url).lower()

            missing_key_client = BinanceRestClient(client=http, api_key="", retries=1)
            with pytest.raises(BinanceDataError, match="BINANCE_API_KEY_REQUIRED"):
                await missing_key_client.fetch_endpoint_json(
                    "/futures/data/topLongShortAccountRatio",
                    {"symbol": "BTCUSDT", "period": "1h", "limit": 1},
                )
            assert len(seen) == 1

    asyncio.run(scenario())
