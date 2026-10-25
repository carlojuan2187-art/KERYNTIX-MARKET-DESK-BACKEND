"""Single Binance Futures WebSocket worker writing live state/events to shared cache.

The parser follows the current bot's market-feed semantics. The worker uses one combined
connection for all-market mini tickers and force orders, then writes shared Redis state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # Environment may be injected by the process manager.
    pass
import time
from typing import Any, Callable

from api.cache import JsonCache, RedisJsonCache
from api.services.market import FEED_STATUS_KEY, FUNDING_KEY, QUOTES_KEY, quote_from_ticker, utc_iso

logger = logging.getLogger("ftrade.market.worker")
WS_BASE = os.getenv("BINANCE_FUTURES_WS", "wss://fstream.binance.com/market").rstrip("/")
STREAMS = "!miniTicker@arr/!forceOrder@arr/!markPrice@arr@1s"
STREAM_URL = f"{WS_BASE}/stream?streams={STREAMS}"
WATCHDOG_SECONDS = max(5.0, float(os.getenv("MARKET_STREAM_WATCHDOG_SECONDS", "15")))
MAX_BACKOFF_SECONDS = 30.0
SPARKLINE_POINTS = 60
LIQUIDATION_RETENTION_SECONDS = 2 * 60 * 60
MAX_LIQUIDATION_EVENTS = 200_000


def _decode(raw: Any) -> Any | None:
    try:
        value = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (TypeError, ValueError):
        return None
    if isinstance(value, dict) and "data" in value:
        return value.get("data")
    return value


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def parse_mini_tickers(raw: Any) -> list[dict[str, Any]]:
    """Extract valid USDⓈ-M mini tickers from raw or combined stream data."""
    message = _decode(raw)
    rows = message if isinstance(message, list) else [message]
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("s") or row.get("symbol") or "").upper()
        if not symbol.endswith("USDT") or "_" in symbol:
            continue
        # Binance's unified ticker feed adds st=1 for USDⓈ-M and st=2 for COIN-M.
        if row.get("st") is not None and str(row.get("st")) != "1":
            continue
        price = _number(row.get("c", row.get("lastPrice")))
        opened = _number(row.get("o", row.get("openPrice")))
        if price is None or price <= 0:
            continue
        normalized = dict(row)
        normalized["s"] = symbol
        normalized["c"] = price
        normalized["o"] = opened if opened and opened > 0 else None
        normalized["h"] = _number(row.get("h", row.get("highPrice")))
        normalized["l"] = _number(row.get("l", row.get("lowPrice")))
        normalized["q"] = _number(row.get("q", row.get("quoteVolume")))
        result.append(normalized)
    return result


def parse_liquidations(raw: Any, received_at: float | None = None) -> list[dict[str, Any]]:
    """Mirror the original bot parser: SELL liquidates longs; BUY liquidates shorts."""
    message = _decode(raw)
    rows = message if isinstance(message, list) else [message]
    now = time.time() if received_at is None else received_at
    out: list[dict[str, Any]] = []
    for event in rows:
        if not isinstance(event, dict) or event.get("e") != "forceOrder":
            continue
        order = event.get("o")
        if not isinstance(order, dict):
            continue
        symbol = str(order.get("s") or "").upper()
        side = str(order.get("S") or "").upper()
        if not symbol.endswith("USDT") or side not in {"SELL", "BUY"}:
            continue
        price = _number(order.get("ap")) or _number(order.get("p"))
        qty = _number(order.get("z")) or _number(order.get("q"))
        if price is None or qty is None or price <= 0 or qty <= 0:
            continue
        amount = price * qty
        if not math.isfinite(amount) or amount <= 0:
            continue
        event_ms = _number(order.get("T")) or _number(event.get("E"))
        event_epoch = event_ms / 1000.0 if event_ms and event_ms > 100_000_000_000 else now
        out.append({
            "timestamp": event_epoch,
            "symbol": symbol,
            "side": "long" if side == "SELL" else "short",
            "order_side": side,
            "price": price,
            "quantity": qty,
            "amount_usdt": amount,
            "created_at": utc_iso(event_epoch),
        })
    return out


def parse_mark_prices(raw: Any) -> list[dict[str, Any]]:
    """Extract mark/index/funding fields from the live mark-price stream."""
    message = _decode(raw)
    rows = message if isinstance(message, list) else [message]
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("e") != "markPriceUpdate":
            continue
        symbol = str(row.get("s") or "").upper()
        if not symbol.endswith("USDT") or "_" in symbol:
            continue
        if row.get("st") is not None and str(row.get("st")) != "1":
            continue
        funding = _number(row.get("r"))
        if funding is None:
            continue
        out.append({
            "symbol": symbol,
            "lastFundingRate": funding,
            "markPrice": _number(row.get("p")),
            "indexPrice": _number(row.get("i")),
            "nextFundingTime": _number(row.get("T")),
            "updated_at": utc_iso(),
        })
    return out


class MarketFeedWorker:
    """Stateful worker; `process_message` is deterministic and independently testable."""
    def __init__(self, cache: JsonCache, *, connect_factory: Callable[[str], Any] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cache = cache
        self.connect_factory = connect_factory
        self.clock = clock
        self.connections = 0
        self.last_event_epoch: float | None = None
        self.connected = False
        self._stop = asyncio.Event()
        self._movers_started_epoch: float | None = None
        self._liquidations_started_epoch: float | None = None
        self._liquidations_connection_count = 0

    async def start(self) -> None:
        self._stop.clear()
        backoff = 1.0
        while not self._stop.is_set():
            began = self.clock()
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Feed Binance desconectado (%s: %s)", type(exc).__name__, exc)
                await self._write_status(connected=False, message=f"WebSocket desconectado: {type(exc).__name__}")
            finally:
                self.connected = False
            if self._stop.is_set():
                break
            if self.clock() - began > 60:
                backoff = 1.0
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2.0, MAX_BACKOFF_SECONDS)

    async def stop(self) -> None:
        self._stop.set()
        await self._write_status(connected=False, message="Worker detenido.")

    async def _session(self) -> None:
        connect = self.connect_factory or self._default_connect()
        async with connect(STREAM_URL) as websocket:
            self.connections += 1
            self.connected = True
            logger.info("Conectado a Binance Futures WebSocket (%s)", STREAM_URL)
            # A reconnect restarts collection coverage, because the gap is not observed.
            self._movers_started_epoch = self.clock()
            self._liquidations_started_epoch = self.clock()
            self._liquidations_connection_count += 1
            await self._write_status(connected=True, message=None)
            await self._write_liquidations_status(connected=True, reset_coverage=True)
            await self._write_movers_collecting()
            try:
                while not self._stop.is_set():
                    raw = await asyncio.wait_for(websocket.recv(), timeout=WATCHDOG_SECONDS)
                    await self.process_message(raw)
            finally:
                self.connected = False
                await self._write_status(connected=False, message="Conexión WebSocket cerrada.")
                await self._write_liquidations_status(connected=False)

    @staticmethod
    def _default_connect() -> Callable[[str], Any]:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise RuntimeError("Instala websockets>=12,<14 para iniciar el worker") from exc
        def connect(url: str):
            return websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22, close_timeout=5)
        return connect

    async def process_message(self, raw: Any) -> dict[str, int]:
        tickers = parse_mini_tickers(raw)
        liquidations = parse_liquidations(raw, received_at=self.clock())
        mark_updates = parse_mark_prices(raw)
        now = self.clock()
        if (tickers or mark_updates or liquidations) and self._movers_started_epoch is None:
            self._movers_started_epoch = now
        updated_quotes = 0
        if tickers:
            previous_map = await self.cache.get_json(QUOTES_KEY)
            quotes = dict(previous_map) if isinstance(previous_map, dict) else {}
            changed: list[dict[str, Any]] = []
            for ticker in tickers:
                symbol = ticker["s"]
                previous = quotes.get(symbol, {})
                previous = previous if isinstance(previous, dict) else {}
                quote = quote_from_ticker(ticker)
                if quote is None:
                    continue
                sparkline = list(previous.get("sparkline", [])) if isinstance(previous.get("sparkline", []), list) else []
                if not sparkline or sparkline[-1] != quote["price"]:
                    sparkline.append(quote["price"])
                quote["sparkline"] = sparkline[-SPARKLINE_POINTS:]
                # Keep the 65-minute, 15-second sample history required by the original
                # MoversEngine; the short sparkline above is only for visual rendering.
                history_key = f"market:price-history:v1:{symbol}"
                history = await self.cache.get_timed_list_json(history_key, limit=1, retention_seconds=3900)
                if not history or now - float(history[0].get("timestamp", 0)) >= 15.0:
                    await self.cache.append_timed_json(
                        history_key,
                        {"timestamp": now, "price": quote["price"], "volume_24h": quote["volume_24h"]},
                        timestamp=now, max_length=300, retention_seconds=3900,
                    )
                # Other real analytical modules can enrich these values later. Preserve them.
                for key in ("funding_rate", "open_interest", "buy_pct", "volume_ratio", "score", "trend", "liquidity_sweep"):
                    if previous.get(key) is not None:
                        quote[key] = previous[key]
                quotes[symbol] = quote
                changed.append({
                    "symbol": symbol, "price": quote["price"], "change_pct": quote["change_pct"],
                    "volume_24h": quote["volume_24h"], "high_24h": quote["high_24h"],
                    "low_24h": quote["low_24h"],
                })
            if changed:
                await self.cache.set_json(QUOTES_KEY, quotes, ttl_seconds=7200)
                self.last_event_epoch = now
                await self.cache.publish_json("market:events", {
                    "channel": "market", "event": "price.update", "payload": changed, "ts": utc_iso(now),
                })
                await self._write_movers_collecting()
                updated_quotes = len(changed)
        if mark_updates:
            existing = await self.cache.get_json(FUNDING_KEY)
            funding_map = {str(row.get("symbol")): row for row in existing if isinstance(row, dict) and row.get("symbol")} if isinstance(existing, list) else {}
            for item in mark_updates:
                funding_map[item["symbol"]] = item
            await self.cache.set_json(FUNDING_KEY, list(funding_map.values()), ttl_seconds=180)
            await self.cache.set_json(f"{FUNDING_KEY}:meta", {"updated_epoch": now, "updated_at": utc_iso(now)}, ttl_seconds=180)
            await self.cache.publish_json("market:events", {
                "channel": "market", "event": "funding.update", "payload": mark_updates, "ts": utc_iso(now),
            })
        if liquidations:
            for item in liquidations:
                await self.cache.append_timed_json(
                    "market:liquidations:events:v1", item,
                    timestamp=float(item["timestamp"]),
                    max_length=MAX_LIQUIDATION_EVENTS,
                    retention_seconds=LIQUIDATION_RETENTION_SECONDS,
                )
                await self.cache.publish_json("market:events", {
                    "channel": "market", "event": "liquidation.new", "payload": item, "ts": utc_iso(now),
                })
        if tickers or mark_updates or liquidations:
            await self._write_liquidations_status(connected=self.connected, last_event_epoch=max(float(row["timestamp"]) for row in liquidations) if liquidations else None)
            await self._write_status(
                connected=self.connected,
                last_price_epoch=now if tickers else None,
                last_message_epoch=now,
                message=None,
            )
        return {"quotes_updated": updated_quotes, "liquidations_received": len(liquidations), "funding_updates": len(mark_updates)}

    async def _write_status(self, *, connected: bool, last_price_epoch: float | None = None,
                            last_message_epoch: float | None = None, message: str | None = None) -> None:
        previous = await self.cache.get_json(FEED_STATUS_KEY) or {}
        now = self.clock()
        latest_price_epoch = last_price_epoch if last_price_epoch is not None else previous.get("last_price_update_epoch")
        latest_message_epoch = last_message_epoch if last_message_epoch is not None else previous.get("last_message_epoch")
        fresh = isinstance(latest_message_epoch, (float, int)) and now - float(latest_message_epoch) <= WATCHDOG_SECONDS
        value = {
            "connected": connected,
            "source": "websocket" if latest_message_epoch is not None else "none",
            "status": "healthy" if connected and fresh else "degraded",
            "updated_at": utc_iso(now),
            "last_message_at": utc_iso(latest_message_epoch) if isinstance(latest_message_epoch, (float, int)) else None,
            "last_message_epoch": latest_message_epoch,
            "last_price_update_at": utc_iso(latest_price_epoch) if isinstance(latest_price_epoch, (float, int)) else None,
            "last_price_update_epoch": latest_price_epoch,
            "message": message,
            "connection_count": self.connections,
        }
        await self.cache.set_json(FEED_STATUS_KEY, value, ttl_seconds=7200)

    async def _write_movers_collecting(self) -> None:
        now = self.clock()
        started = self._movers_started_epoch if self._movers_started_epoch is not None else now
        coverage = max(0.0, (now - started) / 60.0)
        await self.cache.set_json("market:movers:coverage:v1", {
            "started_at_epoch": started,
            "collected_minutes": round(coverage, 3),
            "connected": self.connected,
            "updated_at": utc_iso(now),
        }, ttl_seconds=7200)
        # Keep the original collecting marker for old clients/health checks. The
        # complete mover snapshots are computed by the API from real price history
        # and stored under market:movers:computed:v1:{window}; this marker never
        # pretends that a window is ready before the worker has enough samples.
        for window in (5, 15, 60):
            required = window * 0.9
            existing = await self.cache.get_json(f"market:movers:v1:{window}")
            if not isinstance(existing, dict) or existing.get("status") == "collecting":
                await self.cache.set_json(f"market:movers:v1:{window}", {
                    "window": window,
                    "status": "collecting",
                    "ready": False,
                    "collected_minutes": round(coverage, 3),
                    "required_minutes": required,
                    "gainers": [],
                    "losers": [],
                    "volume_flat": [],
                    "message": "Acumulando historial del feed; el cálculo completo de movers usa las muestras reales del mercado.",
                    "updated_at": utc_iso(now),
                }, ttl_seconds=7200)

    async def _write_liquidations_status(self, *, connected: bool, reset_coverage: bool = False,
                                         last_event_epoch: float | None = None) -> None:
        now = self.clock()
        previous = await self.cache.get_json("market:liquidations:status:v1")
        previous = previous if isinstance(previous, dict) else {}
        started = now if reset_coverage or self._liquidations_started_epoch is None else self._liquidations_started_epoch
        last_event = last_event_epoch if last_event_epoch is not None else previous.get("last_event_epoch")
        value = {
            "connected": bool(connected),
            "started_at_epoch": started,
            "collected_seconds": max(0.0, now - started),
            "last_event_epoch": last_event,
            "last_event_at": utc_iso(last_event) if isinstance(last_event, (int, float)) else None,
            "updated_at": utc_iso(now),
            "connection_count": self._liquidations_connection_count,
            "source": "binance-futures-force-order-stream",
        }
        await self.cache.set_json("market:liquidations:status:v1", value, ttl_seconds=7200)


async def _run_worker() -> None:
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        raise RuntimeError("El worker vive en un proceso separado y requiere REDIS_URL compartido con la API")
    cache = RedisJsonCache.from_url(redis_url)
    worker = MarketFeedWorker(cache)
    try:
        from worker.distributed_lease import DistributedWorkerLease
        async with DistributedWorkerLease(cache, "market-feed"):
            await worker.start()
    finally:
        await worker.stop()
        await cache.close()


if __name__ == "__main__":  # pragma: no cover - manual worker process
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    try:
        asyncio.run(_run_worker())
    except KeyboardInterrupt:
        pass
