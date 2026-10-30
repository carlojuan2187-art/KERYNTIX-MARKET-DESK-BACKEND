"""Market-driven PENDING -> TOUCHED/CANCELLED and open-trade result worker.

Run separately from the API with `python -m worker.signal_lifecycle`. This worker
reuses the original bot's pure lifecycle/outcome rules; only IO and persistence are
implemented here. Notifications are published to Redis for web/API consumers.
"""
from __future__ import annotations

from api.logging_config import configure_logging

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from api.cache import JsonCache, cache_from_environment
from api.repositories.lifecycle import LifecycleRepository, lifecycle_repository_from_environment
from api.services.market import FEED_STATUS_KEY, QUOTES_KEY
from engine.lifecycle_logic import (
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_TOUCHED,
    evaluate_pending_signal,
    utc_naive,
)
from engine.outcome_logic import (
    OPEN_STATES,
    RESULT_INVALID,
    STATE_CLOSED,
    advance_trade,
    build_result_doc,
)
from worker.binance_rest import BinanceRestClient

logger = logging.getLogger("ftrade.signal_lifecycle")

MONITOR_INTERVAL_SECONDS = max(2, int(os.getenv("SIGNAL_MONITOR_INTERVAL_SECONDS", "2")))
MONITOR_ERROR_BACKOFF_SECONDS = max(5, int(os.getenv("SIGNAL_MONITOR_ERROR_BACKOFF_SECONDS", "20")))
HISTORY_RECHECK_SECONDS = max(10, int(os.getenv("SIGNAL_HISTORY_RECHECK_SECONDS", "15")))
OUTCOME_CHECK_SECONDS = max(5, int(os.getenv("OUTCOME_CHECK_SECONDS", "20")))
OUTCOME_MAX_PAGES = max(1, min(100, int(os.getenv("OUTCOME_MAX_PAGES", "10"))))
MARKET_PRICE_MAX_AGE_SECONDS = max(3, int(os.getenv("MARKET_PRICE_MAX_AGE_SECONDS", "15")))
REST_PRICE_REFRESH_SECONDS = max(5, int(os.getenv("LIFECYCLE_REST_PRICE_REFRESH_SECONDS", "10")))
MAX_PENDING_SIGNALS = max(10, min(5000, int(os.getenv("LIFECYCLE_MAX_PENDING_SIGNALS", "2000"))))
MAX_OPEN_TRADES = max(10, min(5000, int(os.getenv("LIFECYCLE_MAX_OPEN_TRADES", "2000"))))
LIFECYCLE_STATUS_KEY = "signal-lifecycle:status:v1"
SIGNAL_EVENTS_CHANNEL = "signals:events:v1"


def _rows_to_candles(rows: list[list[Any]], *, now: datetime, start_at: datetime | None = None,
                     end_at: datetime | None = None) -> list[dict[str, Any]]:
    """Convert Binance klines to closed candle events only."""
    now_naive = utc_naive(now) or datetime.utcnow()
    start_naive = utc_naive(start_at)
    end_naive = utc_naive(end_at)
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            open_at = datetime.fromtimestamp(int(row[0]) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
            close_at = datetime.fromtimestamp(int(row[6]) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
            if close_at > now_naive:
                continue
            if start_naive is not None and close_at < start_naive:
                continue
            if end_naive is not None and close_at > end_naive:
                continue
            candle = {
                "open_time": open_at,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "close_time": close_at,
            }
            if candle["low"] <= 0 or candle["high"] <= 0 or candle["high"] < candle["low"]:
                continue
            result.append(candle)
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
    result.sort(key=lambda item: item["open_time"])
    return result


def _signal_id(signal: Mapping[str, Any]) -> str:
    return str(signal.get("id") or signal.get("signal_id") or signal.get("_id") or "")


def _iso_utc(value: Any) -> str | None:
    dt = utc_naive(value)
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


class SignalLifecycleWorker:
    """One-pass lifecycle/outcome processor with injected IO dependencies."""

    def __init__(
        self,
        market_client: Any,
        repository: LifecycleRepository,
        cache: JsonCache,
        *,
        clock=lambda: datetime.now(timezone.utc),
        interval_seconds: int = MONITOR_INTERVAL_SECONDS,
        history_recheck_seconds: int = HISTORY_RECHECK_SECONDS,
        outcome_check_seconds: int = OUTCOME_CHECK_SECONDS,
        max_pending: int = MAX_PENDING_SIGNALS,
        max_open: int = MAX_OPEN_TRADES,
    ) -> None:
        self.market = market_client
        self.repository = repository
        self.cache = cache
        self.clock = clock
        self.interval_seconds = max(2, int(interval_seconds))
        self.history_recheck_seconds = max(10, int(history_recheck_seconds))
        self.outcome_check_seconds = max(5, int(outcome_check_seconds))
        self.max_pending = max(10, int(max_pending))
        self.max_open = max(10, int(max_open))
        self._stop = asyncio.Event()
        self._last_history_scan: datetime | None = None
        self._last_outcome_scan: datetime | None = None
        self._last_rest_prices_at = 0.0
        self._rest_prices: dict[str, float] = {}

    async def _prices(self, now: datetime) -> dict[str, float]:
        """Use a fresh market-feed cache first; batch REST fallback is rate-limited."""
        try:
            quotes = await self.cache.get_json(QUOTES_KEY)
            status = await self.cache.get_json(FEED_STATUS_KEY) or {}
            last_epoch = status.get("last_price_update_epoch")
            if last_epoch is None:
                last_epoch = status.get("last_message_epoch")
            if isinstance(quotes, dict) and quotes and isinstance(last_epoch, (float, int)):
                age = max(0.0, now.timestamp() - float(last_epoch))
                if age <= MARKET_PRICE_MAX_AGE_SECONDS:
                    cached_prices = {
                        str(symbol).upper(): float(item["price"])
                        for symbol, item in quotes.items()
                        if isinstance(item, Mapping) and _valid_positive(item.get("price"))
                    }
                    if cached_prices:
                        return cached_prices
        except Exception as exc:
            logger.debug("No se pudo leer el feed de precios en caché (%s)", type(exc).__name__)

        epoch = now.timestamp()
        if self._rest_prices and epoch - self._last_rest_prices_at < REST_PRICE_REFRESH_SECONDS:
            return dict(self._rest_prices)
        try:
            rows = await self.market.fetch_24h_tickers()
            latest: dict[str, float] = {}
            for row in rows:
                symbol = str(row.get("symbol") or "").upper()
                price = row.get("lastPrice")
                if symbol and _valid_positive(price):
                    latest[symbol] = float(price)
            if latest:
                self._rest_prices = latest
                self._last_rest_prices_at = epoch
                return dict(latest)
        except Exception as exc:
            logger.warning("No se pudieron obtener precios globales para el ciclo de vida (%s)", type(exc).__name__)
        # Do not make decisions based on indefinitely stale prices.
        if self._rest_prices and epoch - self._last_rest_prices_at <= 60:
            return dict(self._rest_prices)
        return {}

    async def _candles(self, symbol: str, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        start = utc_naive(start_at) or datetime.utcnow() - timedelta(minutes=10)
        end = utc_naive(end_at) or datetime.utcnow()
        rows = await self.market.fetch_klines(
            symbol, "1m", 1000,
            start_time_ms=int(start.replace(tzinfo=timezone.utc).timestamp() * 1000),
            end_time_ms=int(end.replace(tzinfo=timezone.utc).timestamp() * 1000),
        )
        return _rows_to_candles(rows, now=end, start_at=start, end_at=end)

    async def process_pending_once(self, *, now: datetime | None = None) -> int:
        current_time = utc_naive(now or self.clock()) or datetime.utcnow()
        pending = list(self.repository.list_pending(limit=self.max_pending))
        if not pending:
            return 0
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for signal in pending:
            symbol = str(signal.get("symbol") or "").upper()
            if symbol:
                grouped.setdefault(symbol, []).append(signal)
        if not grouped:
            return 0

        try:
            price_map = await self._prices(current_time.replace(tzinfo=timezone.utc))
        except Exception as exc:
            logger.warning("Monitor sin precios actuales (%s); se usará historial disponible", type(exc).__name__)
            price_map = {}

        history_due = self._last_history_scan is None or (current_time - self._last_history_scan).total_seconds() >= self.history_recheck_seconds
        history_by_symbol: dict[str, list[dict[str, Any]]] = {}
        if history_due:
            for symbol, signals in grouped.items():
                created_values = [utc_naive(item.get("created_at") or (item.get("lifecycle") or {}).get("created_at")) for item in signals]
                earliest = min((value for value in created_values if value is not None), default=current_time - timedelta(minutes=20))
                try:
                    history_by_symbol[symbol] = await self._candles(symbol, earliest, current_time)
                except Exception as exc:
                    logger.warning("No se pudo consultar historial de entrada para %s (%s)", symbol, type(exc).__name__)
                    history_by_symbol[symbol] = []
            self._last_history_scan = current_time

        transitions = 0
        for symbol, signals in grouped.items():
            price = price_map.get(symbol)
            candles = history_by_symbol.get(symbol, [])
            for signal in signals:
                try:
                    event = evaluate_pending_signal(dict(signal), candles, now=current_time, current_price=price)
                    if event is None:
                        continue
                    if self.repository.transition_pending(signal, event):
                        transitions += 1
                        await self._publish({
                            "type": "signal.status.updated",
                            "signal_id": _signal_id(signal), "symbol": symbol,
                            "direction": signal.get("direction"),
                            "status": event.get("status"), "reason": event.get("reason"),
                            "event_at": _iso_utc(event.get("event_at") or current_time),
                            "event_price": event.get("event_price"),
                        })
                        logger.info("Señal %s %s -> %s (%s)", symbol, signal.get("direction"), event.get("status"), event.get("reason"))
                except Exception:
                    logger.exception("Error procesando señal pendiente %s", _signal_id(signal))
        return transitions

    async def _fetch_candles_since(self, symbol: str, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        candles: list[dict[str, Any]] = []
        cursor = utc_naive(start_at) or datetime.utcnow()
        end = utc_naive(end_at) or datetime.utcnow()
        for _ in range(OUTCOME_MAX_PAGES):
            rows = await self.market.fetch_klines(
                symbol, "1m", 1000,
                start_time_ms=int(cursor.replace(tzinfo=timezone.utc).timestamp() * 1000),
                end_time_ms=int(end.replace(tzinfo=timezone.utc).timestamp() * 1000),
            )
            if not rows:
                break
            page = _rows_to_candles(rows, now=end, start_at=cursor, end_at=end)
            candles.extend(page)
            if len(rows) < 1000:
                break
            try:
                last_close_ms = int(rows[-1][6])
            except (IndexError, TypeError, ValueError):
                break
            next_cursor = datetime.fromtimestamp((last_close_ms + 1) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            # If Binance included the still-forming candle, there is nothing more to page.
            if cursor >= end:
                break
        # API responses can overlap by one candle at page boundaries; dedupe by open time.
        unique = {candle["open_time"]: candle for candle in candles}
        return sorted(unique.values(), key=lambda candle: candle["open_time"])

    async def track_open_trades_once(self, *, now: datetime | None = None, force: bool = False) -> int:
        current_time = utc_naive(now or self.clock()) or datetime.utcnow()
        if not force and self._last_outcome_scan is not None and (current_time - self._last_outcome_scan).total_seconds() < self.outcome_check_seconds:
            return 0
        self._last_outcome_scan = current_time
        open_signals = list(self.repository.list_open(limit=self.max_open))
        changed = 0
        for signal in open_signals:
            try:
                outcome_before = dict(signal.get("outcome") or {})
                previous_tracked = utc_naive(outcome_before.get("tracked_until")) or utc_naive(signal.get("touched_at"))
                if previous_tracked is None or (current_time - previous_tracked).total_seconds() < 61:
                    continue
                symbol = str(signal.get("symbol") or "").upper()
                try:
                    candles = await self._fetch_candles_since(symbol, previous_tracked + timedelta(milliseconds=1), current_time)
                except Exception as exc:
                    logger.warning("Tracker: error consultando %s (%s)", symbol, type(exc).__name__)
                    continue
                if not candles:
                    continue
                step = advance_trade(dict(signal), candles, now=current_time)
                if not step["advanced"]:
                    continue
                new_outcome = dict(step["outcome"])
                closed = step["closed"]
                levels = step["levels"]
                if new_outcome.get("state") == STATE_CLOSED and closed is not None and levels is not None:
                    result_doc = build_result_doc(dict(signal), levels, closed)
                    # Canonical web IDs are stable UUIDs; the tracker supports older records too.
                    self.repository.save_result_if_absent(result_doc)
                    new_outcome["r_real"] = closed.get("r_real")
                expected_tracked = outcome_before.get("tracked_until") if "tracked_until" in outcome_before else previous_tracked
                updated = self.repository.compare_and_set_outcome(signal, expected_tracked, new_outcome)
                if not updated:
                    continue
                event_count = len(step.get("events") or [])
                for event in step.get("events") or []:
                    await self._publish({
                        "type": "signal.outcome.updated",
                        "signal_id": _signal_id(signal), "symbol": symbol,
                        "direction": signal.get("direction"),
                        "event_type": event.get("type"), "result": event.get("result"),
                        "r": event.get("r"), "r_tp1": event.get("r_tp1"),
                        "ambiguous": event.get("ambiguous", False),
                        "event_at": _iso_utc(event.get("at") or current_time),
                    })
                if new_outcome.get("state") == STATE_CLOSED and new_outcome.get("result") == RESULT_INVALID:
                    logger.warning("Señal %s cerrada INVALID por geometría inutilizable", _signal_id(signal))
                if event_count or new_outcome.get("state") == STATE_CLOSED:
                    changed += 1
            except Exception:
                logger.exception("Tracker: error procesando señal %s", _signal_id(signal))
        return changed

    async def run_once(self) -> dict[str, Any]:
        began = time.monotonic()
        now = utc_naive(self.clock()) or datetime.utcnow()
        pending_count = await self.process_pending_once(now=now)
        outcome_count = await self.track_open_trades_once(now=now)
        status = {
            "status": "ok", "checked_at": _iso_utc(now),
            "pending_transitions": pending_count, "outcome_transitions": outcome_count,
            "duration_seconds": round(time.monotonic() - began, 3),
        }
        try:
            await self.cache.set_json(LIFECYCLE_STATUS_KEY, status, ttl_seconds=3600)
        except Exception as exc:
            logger.debug("No se pudo guardar estado del monitor (%s)", type(exc).__name__)
        return status

    async def _publish(self, event: Mapping[str, Any]) -> None:
        try:
            await self.cache.publish_json(SIGNAL_EVENTS_CHANNEL, dict(event))
        except Exception as exc:
            logger.debug("No se pudo publicar evento de ciclo de vida (%s)", type(exc).__name__)

    async def run_forever(self) -> None:
        self._stop.clear()
        logger.info("Worker de ciclo de vida iniciado; intervalo=%ss", self.interval_seconds)
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error crítico en el worker de ciclo de vida")
                try:
                    await self.cache.set_json(LIFECYCLE_STATUS_KEY, {
                        "status": "degraded", "error": "LIFECYCLE_CYCLE_FAILED", "checked_at": _iso_utc(self.clock()),
                    }, ttl_seconds=3600)
                except Exception:
                    pass
                await asyncio.sleep(MONITOR_ERROR_BACKOFF_SECONDS)
            delay = max(0.0, self.interval_seconds - (time.monotonic() - began))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()


def _valid_positive(value: Any) -> bool:
    try:
        import math
        number = float(value)
        return math.isfinite(number) and number > 0
    except (TypeError, ValueError, OverflowError):
        return False


async def _run_worker() -> None:
    configure_logging()
    repository = lifecycle_repository_from_environment()
    health = repository.health()
    if not health.get("persistent") and os.getenv("LIFECYCLE_ALLOW_MEMORY", "false").lower() not in {"1", "true", "yes"}:
        raise RuntimeError("El worker de ciclo de vida requiere MongoDB persistente en despliegue")
    cache = cache_from_environment()
    rest = BinanceRestClient()
    worker = SignalLifecycleWorker(rest, repository, cache)
    logger.info("Worker de ciclo de vida iniciado; storage=%s", health.get("adapter"))
    try:
        from worker.distributed_lease import DistributedWorkerLease
        async with DistributedWorkerLease(cache, "signal-lifecycle"):
            await worker.run_forever()
    finally:
        await rest.close()
        await cache.close()
        repository.close()


def main() -> None:
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
