"""Bounded Binance USDⓈ-M Futures REST client with retries and no secrets."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger("ftrade.binance.rest")


class BinanceDataError(RuntimeError):
    code = "BINANCE_UNAVAILABLE"


class BinanceInvalidSymbolError(BinanceDataError):
    code = "SYMBOL_NOT_FOUND"


class BinanceRestClient:
    def __init__(self, *, base_url: str = "https://fapi.binance.com", timeout_seconds: float = 8.0,
                 retries: int = 3, client: httpx.AsyncClient | None = None,
                 sleep=asyncio.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retries = max(1, min(int(retries), 5))
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=min(3.0, timeout_seconds)),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"User-Agent": "FTrade-Web/0.4"},
        )
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(4)
        self._sleep = sleep

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        async with self._semaphore:
            for attempt in range(self.retries):
                try:
                    response = await self._client.get(path, params=params)
                    if response.status_code == 400:
                        try:
                            body = response.json()
                        except ValueError:
                            body = {}
                        if isinstance(body, dict) and body.get("code") == -1121:
                            raise BinanceInvalidSymbolError("Binance no reconoce el símbolo solicitado")
                    response.raise_for_status()
                    return response.json()
                except BinanceInvalidSymbolError:
                    raise
                except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError, ValueError) as exc:
                    last_error = exc
                    # Do not retry ordinary client errors; do retry timeouts, 429s and 5xx.
                    if isinstance(exc, httpx.HTTPStatusError):
                        status = exc.response.status_code
                        if status not in (418, 429) and status < 500:
                            break
                    if attempt + 1 < self.retries:
                        await self._sleep(min(0.25 * (2 ** attempt), 1.5))
            logger.warning("Binance REST %s falló: %s", path, last_error)
        raise BinanceDataError(f"No se pudo obtener {path} de Binance") from last_error

    async def fetch_24h_tickers(self) -> list[dict[str, Any]]:
        data = await self._get_json("/fapi/v1/ticker/24hr")
        if not isinstance(data, list):
            raise BinanceDataError("Respuesta de tickers de Binance no válida")
        return [row for row in data if isinstance(row, dict)]

    async def fetch_funding(self) -> list[dict[str, Any]]:
        data = await self._get_json("/fapi/v1/premiumIndex")
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise BinanceDataError("Respuesta de funding de Binance no válida")
        return [row for row in data if isinstance(row, dict)]

    async def fetch_symbol_price(self, symbol: str) -> float:
        """Get the current futures last price for a single symbol."""
        data = await self._get_json("/fapi/v1/ticker/price", {"symbol": symbol.upper()})
        if not isinstance(data, dict):
            raise BinanceDataError("Respuesta de precio de Binance no válida")
        try:
            price = float(data["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BinanceDataError("Binance devolvió un precio no numérico") from exc
        if not (price > 0):
            raise BinanceDataError("Binance devolvió un precio no positivo")
        return price

    async def fetch_klines(
        self, symbol: str, interval: str, limit: int,
        *, start_time_ms: int | None = None, end_time_ms: int | None = None,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {
            "symbol": symbol.upper(), "interval": interval,
            "limit": max(1, min(int(limit), 1000)),
        }
        if start_time_ms is not None:
            params["startTime"] = int(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = int(end_time_ms)
        data = await self._get_json("/fapi/v1/klines", params)
        if not isinstance(data, list):
            raise BinanceDataError("Respuesta de velas de Binance no válida")
        return [row for row in data if isinstance(row, list)]


    async def fetch_endpoint_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Fetch one of the explicitly supported public Futures analytics endpoints.

        Keeping an allowlist prevents service code from turning this client into an
        arbitrary URL fetcher while reusing its retry, timeout and concurrency policy.
        """
        allowed = {
            "/fapi/v1/premiumIndex",
            "/fapi/v1/depth",
            "/futures/data/openInterestHist",
            "/futures/data/globalLongShortAccountRatio",
            "/futures/data/topLongShortPositionRatio",
        }
        if path not in allowed:
            raise ValueError("UNSUPPORTED_ANALYTICS_ENDPOINT")
        return await self._get_json(path, params)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
