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
