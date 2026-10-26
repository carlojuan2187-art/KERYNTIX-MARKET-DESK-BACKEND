"""Market data read service: shared cache first, REST fallback second."""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from api.cache import JsonCache
from api.market_schemas import AssetQuote, Candle, MarketFeedHealth
from worker.binance_rest import BinanceDataError, BinanceRestClient

logger = logging.getLogger("ftrade.market")
QUOTES_KEY = "market:quotes:v1"
FEED_STATUS_KEY = "market:feed:status:v1"
FUNDING_KEY = "market:funding:v1"
QUOTE_FRESH_SECONDS = 15.0
SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,30}USDT$")
INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1D": "1d", "1d": "1d"}


def utc_iso(timestamp: float | int | None = None) -> str:
    dt = datetime.fromtimestamp(timestamp if timestamp is not None else time.time(), tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def base_symbol(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


# Keep the familiar labels already used by the frontend. Unknown assets fall back to
# the actual base ticker; this is display metadata, never invented market data.
DISPLAY_NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "BNB": "BNB",
    "AVAX": "Avalanche", "XRP": "XRP", "DOGE": "Dogecoin", "LINK": "Chainlink",
    "1000PEPE": "Pepe", "ARB": "Arbitrum", "OP": "Optimism", "SUI": "Sui",
    "APT": "Aptos", "NEAR": "NEAR", "TIA": "Celestia", "WIF": "dogwifhat",
    "ADA": "Cardano",
}


def quote_from_ticker(ticker: Mapping[str, Any], funding: Mapping[str, Any] | None = None,
                      sparkline: list[float] | None = None) -> dict[str, Any] | None:
    """Map REST/mini-ticker fields to the UI contract without inventing analytics."""
    symbol = str(ticker.get("symbol") or ticker.get("s") or "").upper()
    if not SYMBOL_RE.fullmatch(symbol) or "_" in symbol:
        return None
    price = _float(ticker.get("lastPrice", ticker.get("c")))
    if price is None or price <= 0:
        return None
    opened = _float(ticker.get("openPrice", ticker.get("o")))
    change = _float(ticker.get("priceChangePercent", ticker.get("P")))
    if change is None and opened and opened > 0:
        change = (price / opened - 1.0) * 100.0
    high = _float(ticker.get("highPrice", ticker.get("h")))
    low = _float(ticker.get("lowPrice", ticker.get("l")))
    quote_vol = _float(ticker.get("quoteVolume", ticker.get("q")))
    funding_rate = _float((funding or {}).get("lastFundingRate", (funding or {}).get("r")))
    return {
        "symbol": symbol,
        "name": DISPLAY_NAMES.get(base_symbol(symbol), base_symbol(symbol)),
        "price": price,
        "change_pct": change,
        "volume_24h": quote_vol,
        "high_24h": high if high is not None and high > 0 else None,
        "low_24h": low if low is not None and low > 0 else None,
        "funding_rate": funding_rate,
        "open_interest": None,
        "buy_pct": None,
        "volume_ratio": None,
        "score": None,
        "trend": {"15M": None, "1H": None, "4H": None},
        "sparkline": list(sparkline or []),
        "liquidity_sweep": None,
    }


class MarketDataService:
    def __init__(self, cache: JsonCache, rest_client: BinanceRestClient, *, clock=time.time) -> None:
        self.cache = cache
        self.rest = rest_client
        self.clock = clock
        self._quotes_refresh_lock = asyncio.Lock()
        self._funding_refresh_lock = asyncio.Lock()

    async def feed_status(self) -> MarketFeedHealth:
        raw = await self.cache.get_json(FEED_STATUS_KEY) or {}
        last_update = raw.get("last_message_epoch", raw.get("last_price_update_epoch"))
        age = max(0.0, self.clock() - float(last_update)) if isinstance(last_update, (int, float)) else None
        connected = bool(raw.get("connected"))
        fresh = age is not None and age <= QUOTE_FRESH_SECONDS
        if connected and fresh:
            status, source = "healthy", "websocket"
        elif raw.get("source") == "rest" and fresh:
            status, source = "degraded", "rest"
        elif age is not None:
            status, source = "degraded", "cache"
        else:
            status, source = "collecting", "none"
        return MarketFeedHealth(
            status=status,
            source=source,
            connected=connected,
            updated_at=raw.get("updated_at"),
            last_price_update_at=raw.get("last_price_update_at"),
            stale_seconds=round(age, 3) if age is not None else None,
            message=raw.get("message"),
        )

    async def quotes(self) -> tuple[list[AssetQuote], MarketFeedHealth]:
        cached = await self.cache.get_json(QUOTES_KEY)
        status = await self.feed_status()
        if self._fresh_cached_quotes(cached, status):
            return await self._quotes_with_cached_funding(cached), status

        # Single refresh lock within this process. Shared Redis is required in production;
        # add a distributed refresh lease if API replicas can all reach REST concurrently.
        async with self._quotes_refresh_lock:
            cached = await self.cache.get_json(QUOTES_KEY)
            status = await self.feed_status()
            if self._fresh_cached_quotes(cached, status):
                return await self._quotes_with_cached_funding(cached), status
            try:
                tickers = await self.rest.fetch_24h_tickers()
                funding_rows = await self._get_funding_if_due()
                funding_map = {str(row.get("symbol")): row for row in funding_rows if row.get("symbol")}
                previous = cached if isinstance(cached, dict) else {}
                result: dict[str, dict[str, Any]] = {}
                for ticker in tickers:
                    symbol = str(ticker.get("symbol") or "").upper()
                    old = previous.get(symbol, {})
                    quote = quote_from_ticker(ticker, funding_map.get(symbol), old.get("sparkline", []) if isinstance(old, dict) else [])
                    if not quote:
                        continue
                    # Never overwrite genuine enrichments from other domain modules with nulls.
                    if isinstance(old, dict):
                        for key in ("open_interest", "buy_pct", "volume_ratio", "score", "trend", "liquidity_sweep"):
                            if old.get(key) is not None:
                                quote[key] = old[key]
                    result[symbol] = quote
                if not result:
                    raise BinanceDataError("Binance no devolvió cotizaciones válidas")
                now = self.clock()
                await self.cache.set_json(QUOTES_KEY, result, ttl_seconds=7200)
                status_obj = {
                    "connected": False, "source": "rest", "status": "degraded",
                    "updated_at": utc_iso(now), "last_message_at": utc_iso(now), "last_message_epoch": now,
                    "last_price_update_at": utc_iso(now), "last_price_update_epoch": now,
                    "message": "Feed WebSocket no disponible; cotizaciones obtenidas por REST.",
                }
                await self.cache.set_json(FEED_STATUS_KEY, status_obj, ttl_seconds=7200)
                health = MarketFeedHealth(
                    status="degraded", source="rest", connected=False,
                    updated_at=status_obj["updated_at"], last_price_update_at=status_obj["last_price_update_at"],
                    stale_seconds=0.0, message=status_obj["message"],
                )
                return self._quotes_from_map(result), health
            except Exception as exc:
                logger.warning("REST fallback para quotes no disponible: %s", exc)
                cached = await self.cache.get_json(QUOTES_KEY)
                status = await self.feed_status()
                if isinstance(cached, dict) and cached:
                    status.status = "degraded"
                    status.source = "cache"
                    status.message = "Se muestra la última cotización guardada; el feed y REST no responden."
                    return await self._quotes_with_cached_funding(cached), status
                raise BinanceDataError("No hay cotizaciones en caché y Binance REST no está disponible") from exc

    @staticmethod
    def _fresh_cached_quotes(cached: Any, status: MarketFeedHealth) -> bool:
        # Fresh Redis quotes from the WebSocket need no REST requests. Fresh REST fallback
        # results are also reused briefly to prevent every visitor from hitting Binance.
        return isinstance(cached, dict) and bool(cached) and status.source in {"websocket", "rest"} and status.stale_seconds is not None and status.stale_seconds <= QUOTE_FRESH_SECONDS

    async def _quotes_with_cached_funding(self, raw_map: Mapping[str, Any]) -> list[AssetQuote]:
        """Merge the worker's cached funding stream into price quotes, without REST per request."""
        funding_rows = await self.cache.get_json(FUNDING_KEY)
        funding_map = {
            str(row.get("symbol")): row for row in funding_rows
            if isinstance(row, dict) and row.get("symbol")
        } if isinstance(funding_rows, list) else {}
        enriched: dict[str, dict[str, Any]] = {}
        for symbol, raw in raw_map.items():
            if not isinstance(raw, Mapping):
                continue
            quote = dict(raw)
            funding = funding_map.get(symbol)
            if funding and funding.get("lastFundingRate") is not None:
                try:
                    quote["funding_rate"] = float(funding["lastFundingRate"])
                except (TypeError, ValueError):
                    pass
            enriched[symbol] = quote
        return self._quotes_from_map(enriched)

    async def candles(self, symbol: str, timeframe: str, limit: int, tz_name: str) -> tuple[list[Candle], MarketFeedHealth]:
        normalized = symbol.strip().upper()
        if not SYMBOL_RE.fullmatch(normalized):
            raise ValueError("INVALID_SYMBOL")
        interval = INTERVALS.get(timeframe)
        if interval is None:
            raise ValueError("INVALID_TIMEFRAME")
        try:
            zone = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("INVALID_TIMEZONE") from exc
        cache_key = f"market:candles:v1:{normalized}:{interval}:{limit}:{tz_name}"
        raw_cache = await self.cache.get_json(cache_key)
        cached_rows: list[Any] | None = None
        cached_epoch: float | None = None
        if isinstance(raw_cache, dict) and isinstance(raw_cache.get("candles"), list):
            cached_rows = raw_cache["candles"]
            try:
                cached_epoch = float(raw_cache.get("fetched_epoch", 0))
            except (TypeError, ValueError):
                cached_epoch = None
        elif isinstance(raw_cache, list):  # compatibility with the first phase-4 draft
            cached_rows = raw_cache
            cached_epoch = self.clock()
        cache_age = self.clock() - cached_epoch if cached_epoch is not None else None
        if cached_rows and cache_age is not None and 0 <= cache_age <= 20:
            status = await self.feed_status()
            status.source = "cache"
            status.message = "Velas recientes servidas desde caché REST."
            if status.status == "collecting":
                status.status = "healthy"
            return [Candle.model_validate(row) for row in cached_rows], status
        try:
            raw_rows = await self.rest.fetch_klines(normalized, interval, limit)
            candles = self._convert_klines(raw_rows, zone)
            if not candles:
                raise BinanceDataError("Binance no devolvió velas válidas")
            now_epoch = self.clock()
            payload = {"fetched_epoch": now_epoch, "fetched_at": utc_iso(now_epoch), "candles": [c.model_dump() for c in candles]}
            # Keep stale-if-error data for up to one hour; only serve it as fresh for 20s.
            await self.cache.set_json(cache_key, payload, ttl_seconds=3600)
            now = utc_iso(now_epoch)
            return candles, MarketFeedHealth(
                status="healthy", source="rest", connected=False, updated_at=now,
                last_price_update_at=now, stale_seconds=0.0,
                message="Velas obtenidas por REST y guardadas en caché.",
            )
        except BinanceInvalidSymbolError:
            raise
        except Exception as exc:
            logger.warning("REST fallback para velas %s %s no disponible: %s", normalized, interval, exc)
            if cached_rows:
                status = await self.feed_status()
                status.status, status.source = "degraded", "cache"
                status.message = "Se muestran las últimas velas guardadas; Binance REST no está disponible."
                return [Candle.model_validate(row) for row in cached_rows], status
            if isinstance(exc, BinanceDataError):
                raise
            raise BinanceDataError("No hay velas en caché y Binance REST no está disponible") from exc

    async def _get_funding_if_due(self) -> list[dict[str, Any]]:
        existing = await self.cache.get_json(FUNDING_KEY)
        meta = await self.cache.get_json(f"{FUNDING_KEY}:meta")
        now = self.clock()
        if isinstance(existing, list) and isinstance(meta, dict) and now - float(meta.get("updated_epoch", 0)) < 30:
            return existing
        async with self._funding_refresh_lock:
            try:
                rows = await self.rest.fetch_funding()
                await self.cache.set_json(FUNDING_KEY, rows, ttl_seconds=180)
                await self.cache.set_json(f"{FUNDING_KEY}:meta", {"updated_epoch": now, "updated_at": utc_iso(now)}, ttl_seconds=180)
                return rows
            except Exception:
                return existing if isinstance(existing, list) else []

    @staticmethod
    def _quotes_from_map(raw_map: Mapping[str, Any]) -> list[AssetQuote]:
        result: list[AssetQuote] = []
        for symbol, raw in raw_map.items():
            if not isinstance(raw, Mapping):
                continue
            try:
                item = dict(raw)
                item.setdefault("symbol", symbol)
                result.append(AssetQuote.model_validate(item))
            except Exception as exc:
                logger.warning("Omitiendo cotización inválida %s: %s", symbol, exc)
        return sorted(result, key=lambda row: row.symbol)

    @staticmethod
    def _convert_klines(rows: list[Any], zone: ZoneInfo) -> list[Candle]:
        result: list[Candle] = []
        cvd_total = 0.0
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 11:
                continue
            try:
                timestamp = int(row[0])
                open_price, high, low, close = [float(row[i]) for i in range(1, 5)]
                quote_volume = float(row[7])
                taker_buy_quote = float(row[10])
                values = (open_price, high, low, close, quote_volume, taker_buy_quote)
                if timestamp < 0 or min(open_price, high, low, close) <= 0 or quote_volume < 0 or taker_buy_quote < 0:
                    continue
                if not all(math.isfinite(value) for value in values):
                    continue
                # Flow delta is taker-buy quote volume minus taker-sell quote volume.
                cvd_total += 2.0 * taker_buy_quote - quote_volume
                local_dt = datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc).astimezone(zone)
                result.append(Candle(
                    time=local_dt.strftime("%Y-%m-%d %H:%M"), timestamp=timestamp,
                    open=open_price, high=high, low=low, close=close,
                    volume=quote_volume, cvd=cvd_total,
                ))
            except (TypeError, ValueError, OverflowError):
                continue
        result.sort(key=lambda candle: candle.timestamp)
        return result

    async def close(self) -> None:
        await self.rest.close()
