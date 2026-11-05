"""Radar, liquidation, movers and dashboard-aggregate services.

Reads the shared market feed. The HTTP API never opens a second market WebSocket;
all live liquidation events and price history are produced by worker.market_feed.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pandas as pd
from fastapi.encoders import jsonable_encoder

from api.cache import JsonCache
from api.repositories.signals import SignalRepository
from api.repositories.sweeps import SweepStore
from api.services.market import FEED_STATUS_KEY, FUNDING_KEY, QUOTES_KEY, utc_iso
from engine import flow as flow_engine
from engine.regime import analyze_timeframe
from engine import liquidations as liquidation_engine
from engine import movers as movers_engine
from engine import radar as radar_engine
from worker.binance_rest import BinanceDataError, BinanceRestClient

logger = logging.getLogger("ftrade.market_intelligence")

RADAR_KEY = "market:radar:v1"
SIGNAL_RECORDS_KEY = "market:signal-records:v1"
LIQ_EVENTS_KEY = "market:liquidations:events:v1"
LIQ_STATUS_KEY = "market:liquidations:status:v1"
MOVERS_COVERAGE_KEY = "market:movers:coverage:v1"
REGIME_KEY = "market:regime:v1"
RADAR_TTL = max(20, int(os.getenv("RADAR_CACHE_SECONDS", "60")))
RADAR_REFRESH_RETRY_SECONDS = max(10, int(os.getenv("RADAR_REFRESH_RETRY_SECONDS", "30")))
LIQ_TTL = 5
MOVERS_TTL = 15
RADAR_SYMBOL_LIMIT = max(8, min(30, int(os.getenv("RADAR_SYMBOL_LIMIT", "12"))))
RADAR_CONCURRENCY = max(1, min(6, int(os.getenv("RADAR_CONCURRENCY", "4"))))
TIMEFRAME_TREND_TTL = max(30, min(300, int(os.getenv("RADAR_TIMEFRAME_TREND_TTL", "60"))))
MOVERS_SYMBOL_LIMIT = max(40, min(200, int(os.getenv("MOVERS_SYMBOL_LIMIT", "120"))))
LIQ_EVENT_LIMIT = max(1000, min(100000, int(os.getenv("LIQUIDATION_API_EVENT_LIMIT", "30000"))))


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _quote_volume(row: Mapping[str, Any]) -> float:
    return _finite(row.get("volume_24h", row.get("quote_volume", row.get("quoteVolume", row.get("q", 0))))) or 0.0


def _frame_from_klines(rows: list[list[Any]]) -> pd.DataFrame:
    columns = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_time"])
    width = len(rows[0])
    frame = pd.DataFrame(rows, columns=columns[:width])
    required = {"open", "high", "low", "close", "volume", "close_time"}
    if not required.issubset(frame.columns):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_time"])
    frame["close_time"] = pd.to_datetime(frame["close_time"], unit="ms", utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame[["open", "high", "low", "close", "volume", "close_time"]].dropna().reset_index(drop=True)


def _trend_label(analysis: Any) -> str | None:
    """Translate an actual per-timeframe regime analysis into a compact UI state."""
    if not isinstance(analysis, Mapping):
        return None
    regime = str(analysis.get("regime") or "").upper()
    return {
        "UP": "up",
        "DOWN": "down",
        "RANGE": "range",
        "TRANSITION": "transition",
        "VOLATILE": "volatile",
    }.get(regime)


def _timeframe_trends_from_rows(rows_by_interval: Mapping[str, list[list[Any]]]) -> dict[str, str | None]:
    """Compute 15M/1H/4H trend states from closed Binance Futures klines."""
    result: dict[str, str | None] = {}
    for interval, label in (("15m", "15M"), ("1h", "1H"), ("4h", "4H")):
        try:
            analysis = analyze_timeframe(_frame_from_klines(rows_by_interval.get(interval) or []))
        except Exception:
            logger.debug("No se pudo calcular tendencia %s", label, exc_info=True)
            analysis = None
        result[label] = _trend_label(analysis)
    return result


class MarketIntelligenceService:
    def __init__(self, cache: JsonCache, rest: BinanceRestClient, repository: SignalRepository,
                 sweep_store: SweepStore, *, clock=time.time) -> None:
        self.cache = cache
        self.rest = rest
        self.repository = repository
        self.sweep_store = sweep_store
        self.clock = clock
        self._radar_lock = asyncio.Lock()
        self._radar_refresh_task: asyncio.Task | None = None
        self._radar_retry_after = 0.0
        self._movers_locks = {window: asyncio.Lock() for window in movers_engine.WINDOWS_MINUTES}
        self._movers_refresh_tasks: dict[int, asyncio.Task] = {}
        self._movers_retry_after: dict[int, float] = {}
        self._timeframe_trend_locks: dict[str, asyncio.Lock] = {}
        self._timeframe_trend_semaphore = asyncio.Semaphore(RADAR_CONCURRENCY)
        self._timeframe_trend_refresh_tasks: dict[str, asyncio.Task] = {}
        self._timeframe_trend_retry_after: dict[str, float] = {}
        self._signal_records_lock = asyncio.Lock()
        self._signal_records_refresh_task: asyncio.Task | None = None
        self._signal_records_retry_after = 0.0

    async def signal_records(
        self, *, wait_seconds: float = 0.0,
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        """Serve one shared, short-lived snapshot of live signal candidates.

        Mongo is read once per refresh, not once per visitor or once per screen.
        A last-good snapshot is served while refresh runs; a cold request can wait
        only for a small bounded interval before returning a warming state.
        """
        cached = await self.cache.get_json(SIGNAL_RECORDS_KEY)
        now = self.clock()
        if isinstance(cached, dict) and isinstance(cached.get("rows"), list):
            try:
                age = max(0.0, now - float(cached.get("updated_epoch", 0)))
            except (TypeError, ValueError, OverflowError):
                age = 9999.0
            if age <= 2.0:
                return cached["rows"], False, str(cached.get("updated_at") or "")
            self._schedule_signal_records_refresh()
            if age <= 10.0:
                # A very recent last-good feed is safe to preserve during a short
                # refresh. Older pending signals must not be presented as current.
                return cached["rows"], True, str(cached.get("updated_at") or "")

        task = self._schedule_signal_records_refresh()
        if wait_seconds > 0 and task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=min(2.0, max(0.0, wait_seconds)))
            except Exception:
                # The last-known/empty state remains available; do not make the
                # HTTP request inherit Mongo's full server-selection timeout.
                pass
            cached = await self.cache.get_json(SIGNAL_RECORDS_KEY)
            if isinstance(cached, dict) and isinstance(cached.get("rows"), list):
                return cached["rows"], False, str(cached.get("updated_at") or "")
        cached = await self.cache.get_json(SIGNAL_RECORDS_KEY)
        if isinstance(cached, dict) and isinstance(cached.get("rows"), list):
            try:
                age = max(0.0, self.clock() - float(cached.get("updated_epoch", 0)))
            except (TypeError, ValueError, OverflowError):
                age = 9999.0
            if age <= 10.0:
                return cached["rows"], True, str(cached.get("updated_at") or "")
        return [], True, None

    def _schedule_signal_records_refresh(self) -> asyncio.Task | None:
        task = self._signal_records_refresh_task
        if task is not None and not task.done():
            return task
        if self.clock() < self._signal_records_retry_after:
            return task
        try:
            task = asyncio.create_task(self._refresh_signal_records())
        except RuntimeError:
            return None
        self._signal_records_refresh_task = task

        def finished(done: asyncio.Task) -> None:
            if self._signal_records_refresh_task is done:
                self._signal_records_refresh_task = None
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                self._signal_records_retry_after = self.clock() + 15.0
                logger.warning("No se pudo refrescar el snapshot compartido de señales: %s", error)
            else:
                self._signal_records_retry_after = 0.0

        task.add_done_callback(finished)
        return task

    async def _refresh_signal_records(self) -> None:
        async with self._signal_records_lock:
            try:
                cutoff = datetime.fromtimestamp(self.clock(), tz=timezone.utc) - timedelta(minutes=30)
                reader = getattr(self.repository, "list_live_candidates", None)
                if callable(reader):
                    rows = await asyncio.wait_for(
                        asyncio.to_thread(reader, limit=80, cancelled_since=cutoff),
                        timeout=5.0,
                    )
                else:
                    rows = await asyncio.wait_for(
                        asyncio.to_thread(self.repository.list_signals, limit=100),
                        timeout=5.0,
                    )
                safe_rows: list[dict[str, Any]] = []
                for raw in rows:
                    row = dict(raw)
                    if row.get("_id") is not None and not row.get("id"):
                        row["id"] = str(row["_id"])
                    row.pop("_id", None)
                    encoded = jsonable_encoder(row)
                    if isinstance(encoded, dict):
                        safe_rows.append(encoded)
                now = self.clock()
                snapshot = {
                    "rows": safe_rows,
                    "updated_at": utc_iso(now),
                    "updated_epoch": now,
                }
                await self.cache.set_json(SIGNAL_RECORDS_KEY, snapshot, ttl_seconds=180)
                self._signal_records_retry_after = 0.0
            except Exception:
                self._signal_records_retry_after = self.clock() + 15.0
                logger.warning("Actualización de señales agotó su plazo o falló", exc_info=True)

    @staticmethod
    def _warming_radar_snapshot(now: float) -> dict[str, Any]:
        return {
            "ts": now, "scanned": 0, "prices": {}, "funding": {},
            "watch": [], "watch_total": 0, "sweeps": [], "movers": [],
            "items": [], "timeframe_trends": {}, "status": "warming",
            "analysis_errors": 0, "updated_at": None, "updated_epoch": 0,
            "source": "background-radar-refresh", "refreshing": True,
        }

    def _schedule_radar_refresh(self) -> None:
        task = self._radar_refresh_task
        if task is not None and not task.done():
            return
        if self.clock() < self._radar_retry_after:
            return
        try:
            task = asyncio.create_task(self._build_radar_snapshot(force_refresh=True))
        except RuntimeError:
            logger.warning("No se pudo programar la actualización del radar: no hay event loop activo")
            return
        self._radar_refresh_task = task

        def _report_failure(done: asyncio.Task) -> None:
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                self._radar_retry_after = self.clock() + RADAR_REFRESH_RETRY_SECONDS
                logger.warning("La actualización en segundo plano del radar falló: %s", error)
            else:
                self._radar_retry_after = 0.0

        task.add_done_callback(_report_failure)

    async def radar(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Return a cached radar immediately; build cold/stale snapshots in background.

        The market scan can request dozens of Binance candle series. It must not be
        tied to a user's 9-second page-load deadline. A stale snapshot is preferable
        to a blank/error panel while one shared refresh is in progress.
        """
        now = self.clock()
        if force_refresh:
            return await self._build_radar_snapshot(force_refresh=True)

        cached = await self.cache.get_json(RADAR_KEY)
        if isinstance(cached, dict):
            age = now - float(cached.get("updated_epoch", 0) or 0)
            if 0 <= age < RADAR_TTL:
                return cached
            self._schedule_radar_refresh()
            stale = dict(cached)
            stale["refreshing"] = True
            stale["snapshot_age_seconds"] = max(0.0, age)
            return stale

        self._schedule_radar_refresh()
        return self._warming_radar_snapshot(now)

    async def _build_radar_snapshot(self, *, force_refresh: bool = False) -> dict[str, Any]:
        now = self.clock()
        async with self._radar_lock:
            cached = await self.cache.get_json(RADAR_KEY)
            if not force_refresh and isinstance(cached, dict):
                age = now - float(cached.get("updated_epoch", 0) or 0)
                if 0 <= age < RADAR_TTL:
                    return cached

            quotes = await self._quotes_map()
            if not quotes:
                raise AnalyticsIntelligenceUnavailable("RADAR_QUOTES_UNAVAILABLE")
            sorted_quotes = sorted(quotes.items(), key=lambda pair: _quote_volume(pair[1]), reverse=True)
            symbols = [
                symbol for symbol, row in sorted_quotes
                if symbol.endswith("USDT") and _quote_volume(row) >= movers_engine.MIN_QUOTE_VOLUME
            ][:RADAR_SYMBOL_LIMIT]

            # Load the non-candle inputs once. They are reused for incremental
            # snapshots so early results can be displayed before the full scan ends.
            sweeps: list[dict[str, Any]] = []
            try:
                active = await asyncio.to_thread(
                    self.sweep_store.load_active,
                    now=datetime.fromtimestamp(now, tz=timezone.utc),
                )
                sweeps = list(active.values()) if isinstance(active, dict) else []
            except Exception as exc:
                logger.warning("Radar: observaciones de barrido no disponibles: %s", exc)
            funding_rows = await self.cache.get_json(FUNDING_KEY)
            funding = {
                str(row.get("symbol")): _finite(row.get("lastFundingRate"))
                for row in funding_rows if isinstance(row, dict) and row.get("symbol")
                and _finite(row.get("lastFundingRate")) is not None
            } if isinstance(funding_rows, list) else {}
            regime = await self.cache.get_json(REGIME_KEY)
            semaphore = asyncio.Semaphore(RADAR_CONCURRENCY)
            errors = 0

            async def analyze(symbol: str) -> dict[str, Any] | None:
                nonlocal errors
                async with semaphore:
                    try:
                        fetched = await asyncio.wait_for(
                            asyncio.gather(
                                self.rest.fetch_klines(symbol, "1h", 80),
                                self.rest.fetch_klines(symbol, "15m", 160),
                                self.rest.fetch_klines(symbol, "5m", 60),
                                self.rest.fetch_klines(symbol, "4h", 80),
                                return_exceptions=True,
                            ),
                            timeout=12.0,
                        )
                        # 4H is supplementary; a failure there does not erase a
                        # valid 15M/1H/5M opportunity.
                        required_errors = [value for value in fetched[:3] if isinstance(value, Exception)]
                        if required_errors:
                            raise required_errors[0]
                        f1h, f15m, f5m = fetched[:3]
                        f4h = fetched[3] if not isinstance(fetched[3], Exception) else []
                        item = radar_engine.analyze_symbol(
                            symbol, _frame_from_klines(f1h), _frame_from_klines(f15m), _frame_from_klines(f5m),
                        )
                        trend_rows = {"15m": f15m, "1h": f1h, "4h": f4h}
                        trend_values = await asyncio.to_thread(_timeframe_trends_from_rows, trend_rows)
                        if any(value is not None for value in trend_values.values()):
                            try:
                                await self.cache.set_json(
                                    f"market:timeframe-trends:v1:{symbol}",
                                    {
                                        "trends": trend_values,
                                        "updated_at": utc_iso(self.clock()),
                                        "updated_epoch": self.clock(),
                                        "source": "binance-futures-klines",
                                    },
                                    ttl_seconds=TIMEFRAME_TREND_TTL,
                                )
                            except Exception:
                                logger.debug("No se pudo guardar tendencia multi-temporalidad de %s", symbol, exc_info=True)
                        if item:
                            item["timeframe_trend"] = trend_values
                            last_price = _finite(quotes.get(symbol, {}).get("price"))
                            if last_price is not None:
                                item["price"] = last_price
                        return item
                    except Exception as exc:
                        errors += 1
                        logger.debug("Radar: no se pudo analizar %s: %s", symbol, exc)
                        return None

            async def save_snapshot(rows: list[dict[str, Any]], *, refreshing: bool) -> dict[str, Any]:
                current = self.clock()
                current_dt = datetime.fromtimestamp(current, tz=timezone.utc)
                snapshot = radar_engine.build_radar_snapshot(rows, sweeps, funding, now=current_dt)
                trends_by_symbol = {item.get("symbol"): item.get("trend_1h") for item in rows if item}
                enriched_sweeps = []
                for sweep in snapshot.get("sweeps", []):
                    alignment = radar_engine.alignment_for(
                        sweep["direction"],
                        regime if isinstance(regime, dict) else None,
                        trends_by_symbol.get(sweep["symbol"]),
                    )
                    enriched_sweeps.append({
                        **sweep,
                        "alignment": alignment,
                        "score": radar_engine.sweep_score(sweep, alignment["status"], current),
                    })
                snapshot["sweeps"] = enriched_sweeps
                snapshot["items"] = rows
                snapshot["timeframe_trends"] = {
                    str(item.get("symbol")): item.get("timeframe_trend")
                    for item in rows
                    if item.get("symbol") and isinstance(item.get("timeframe_trend"), dict)
                }
                snapshot["status"] = (
                    "warming" if refreshing and not snapshot["scanned"]
                    else "partial" if refreshing or errors
                    else "ok"
                )
                if not refreshing and errors and not snapshot["scanned"]:
                    snapshot["status"] = "degraded"
                snapshot["refreshing"] = refreshing
                snapshot["analysis_errors"] = errors
                snapshot["updated_at"] = utc_iso(current)
                snapshot["updated_epoch"] = current
                snapshot["source"] = "binance-futures-klines-and-observed-sweeps"
                await self.cache.set_json(RADAR_KEY, snapshot, ttl_seconds=RADAR_TTL * 2)
                return snapshot

            # Publish a partial snapshot every two completed pairs. Radar visitors
            # can see real opportunities as soon as a small batch is ready instead
            # of waiting for every Binance candle request across the whole universe.
            tasks = [asyncio.create_task(analyze(symbol)) for symbol in symbols]
            items: list[dict[str, Any]] = []
            completed = 0
            last_snapshot: dict[str, Any] | None = None
            for task in asyncio.as_completed(tasks):
                item = await task
                completed += 1
                if item:
                    items.append(item)
                if completed % 2 == 0 or completed == len(tasks):
                    last_snapshot = await save_snapshot(items, refreshing=completed < len(tasks))

            if completed == 0:
                last_snapshot = await save_snapshot(items, refreshing=False)
            return last_snapshot or await save_snapshot(items, refreshing=False)

    def _schedule_timeframe_trend_refresh(self, symbol: str) -> asyncio.Task | None:
        task = self._timeframe_trend_refresh_tasks.get(symbol)
        if task is not None and not task.done():
            return task
        if self.clock() < self._timeframe_trend_retry_after.get(symbol, 0.0):
            return task
        try:
            task = asyncio.create_task(self._refresh_timeframe_trend(symbol))
        except RuntimeError:
            return None
        self._timeframe_trend_refresh_tasks[symbol] = task

        def finished(done: asyncio.Task) -> None:
            if self._timeframe_trend_refresh_tasks.get(symbol) is done:
                self._timeframe_trend_refresh_tasks.pop(symbol, None)
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                self._timeframe_trend_retry_after[symbol] = self.clock() + 15.0
                logger.debug("Tendencias no disponibles para %s: %s", symbol, error)

        task.add_done_callback(finished)
        return task

    async def _refresh_timeframe_trend(self, symbol: str) -> None:
        cache_key = f"market:timeframe-trends:v1:{symbol}"
        async with self._timeframe_trend_locks.setdefault(symbol, asyncio.Lock()):
            cached = await self.cache.get_json(cache_key)
            if isinstance(cached, dict) and isinstance(cached.get("trends"), dict):
                current = cached["trends"]
                if all(current.get(label) is not None for label in ("15M", "1H", "4H")):
                    return
            try:
                async with self._timeframe_trend_semaphore:
                    fetched = await asyncio.wait_for(
                        asyncio.gather(
                            self.rest.fetch_klines(symbol, "15m", 160),
                            self.rest.fetch_klines(symbol, "1h", 80),
                            self.rest.fetch_klines(symbol, "4h", 80),
                            return_exceptions=True,
                        ),
                        timeout=10.0,
                    )
            except asyncio.TimeoutError:
                self._timeframe_trend_retry_after[symbol] = self.clock() + 15.0
                logger.debug("Timeout de tendencias multi-temporalidad para %s", symbol)
                return
            rows_by_interval = {
                interval: (fetched[index] if not isinstance(fetched[index], Exception) else [])
                for index, interval in enumerate(("15m", "1h", "4h"))
            }
            trends = await asyncio.to_thread(_timeframe_trends_from_rows, rows_by_interval)
            # Do not cache an all-null result for a full TTL. A provider outage or
            # insufficient history should be retried soon rather than freezing dashes.
            if not any(value is not None for value in trends.values()):
                self._timeframe_trend_retry_after[symbol] = self.clock() + 15.0
                return
            now = self.clock()
            await self.cache.set_json(cache_key, {
                "trends": trends,
                "updated_at": utc_iso(now),
                "updated_epoch": now,
                "source": "binance-futures-klines",
            }, ttl_seconds=TIMEFRAME_TREND_TTL)
            self._timeframe_trend_retry_after.pop(symbol, None)

    async def timeframe_trends_for_symbols(
        self, symbols: list[str] | tuple[str, ...] | set[str],
    ) -> dict[str, dict[str, str | None]]:
        """Return cached 15M/1H/4H trends and schedule missing reads off-request.

        A page render never waits for Binance. Missing values are explicitly null
        and are filled by the background refresh; the frontend polls while pending.
        """
        cleaned: list[str] = []
        for raw_symbol in symbols:
            symbol = str(raw_symbol or "").strip().upper()
            if symbol.endswith("USDT") and "_" not in symbol and symbol not in cleaned:
                cleaned.append(symbol)

        async def for_symbol(symbol: str) -> tuple[str, dict[str, str | None]]:
            cache_key = f"market:timeframe-trends:v1:{symbol}"
            try:
                cached = await self.cache.get_json(cache_key)
            except Exception:
                cached = None
            if isinstance(cached, dict) and isinstance(cached.get("trends"), dict):
                current = {
                    label: cached["trends"].get(label)
                    for label in ("15M", "1H", "4H")
                }
                if any(value is None for value in current.values()):
                    self._schedule_timeframe_trend_refresh(symbol)
                return symbol, current
            self._schedule_timeframe_trend_refresh(symbol)
            return symbol, {"15M": None, "1H": None, "4H": None}

        rows = await asyncio.gather(*(for_symbol(symbol) for symbol in cleaned), return_exceptions=True)
        result: dict[str, dict[str, str | None]] = {}
        for symbol, row in zip(cleaned, rows):
            if isinstance(row, Exception):
                result[symbol] = {"15M": None, "1H": None, "4H": None}
            else:
                result[symbol] = row[1]
        return result

    def timeframe_trends_refreshing(self, symbols: list[str] | tuple[str, ...] | set[str]) -> bool:
        return any(
            (task := self._timeframe_trend_refresh_tasks.get(str(symbol).upper())) is not None and not task.done()
            for symbol in symbols
        )

    async def liquidations(self, window_minutes: int = 15) -> dict[str, Any]:
        if window_minutes not in (5, 15, 60):
            raise ValueError("INVALID_LIQUIDATIONS_WINDOW")
        now = self.clock()
        cache_key = f"market:liquidations:summary:v1:{window_minutes}"
        cached = await self.cache.get_json(cache_key)
        if isinstance(cached, dict) and now - float(cached.get("updated_epoch", 0) or 0) < LIQ_TTL:
            return cached
        events = await self.cache.get_timed_list_json(LIQ_EVENTS_KEY, limit=LIQ_EVENT_LIMIT, retention_seconds=7200)
        status = await self.cache.get_json(LIQ_STATUS_KEY)
        status = status if isinstance(status, dict) else {}
        feed = await self.cache.get_json(FEED_STATUS_KEY)
        feed = feed if isinstance(feed, dict) else {}
        summary = liquidation_engine.summarize_events(events, window_minutes, now=now)
        coverage_started = _finite(status.get("started_at_epoch"))
        coverage = max(0.0, now - coverage_started) if coverage_started else 0.0
        pace = None
        if coverage >= window_minutes * 60 * 3:
            max_previous_windows = min(6, max(0, int(coverage // (window_minutes * 60)) - 1))
            previous = [liquidation_engine.summarize_events(events, window_minutes, now=now, offset_minutes=window_minutes * offset)["total"] for offset in range(1, max_previous_windows + 1)]
            if len(previous) >= 2 and sum(previous) > 0:
                average = sum(previous) / len(previous)
                pace = summary["total"] / average
        biggest = summary.get("biggest")
        output = {key: value for key, value in summary.items() if key not in {"events", "biggest"}}
        output["biggest"] = biggest
        output["pace"] = pace
        output["interpretation"] = liquidation_engine.interpret_liquidations(summary, pace)
        output["coverage_seconds"] = coverage
        output["coverage_complete"] = coverage >= window_minutes * 60
        output["connected"] = bool(status.get("connected") and feed.get("connected"))
        output["last_event_at"] = status.get("last_event_at")
        output["feed_status"] = str(feed.get("status") or "unavailable")
        output["last_feed_message_at"] = feed.get("last_message_at")
        output["status"] = "ok" if output["coverage_complete"] else "collecting"
        output["message"] = None if output["coverage_complete"] else "Recopilando historial; esta ventana todavía no tiene cobertura completa."
        output["source"] = "binance-futures-force-order-stream"
        output["updated_at"] = utc_iso(now)
        output["updated_epoch"] = now
        await self.cache.set_json(cache_key, output, ttl_seconds=LIQ_TTL)
        return output

    def _schedule_movers_refresh(self, window_minutes: int) -> None:
        task = self._movers_refresh_tasks.get(window_minutes)
        if task is not None and not task.done():
            return
        if self.clock() < self._movers_retry_after.get(window_minutes, 0.0):
            return
        try:
            task = asyncio.create_task(self.movers(window_minutes, force_refresh=True))
        except RuntimeError:
            return
        self._movers_refresh_tasks[window_minutes] = task

        def finished(done: asyncio.Task) -> None:
            if self._movers_refresh_tasks.get(window_minutes) is done:
                self._movers_refresh_tasks.pop(window_minutes, None)
            if done.cancelled():
                return
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                self._movers_retry_after[window_minutes] = self.clock() + 15.0
                logger.debug("No se pudo refrescar movers %sm: %s", window_minutes, error)
            else:
                self._movers_retry_after.pop(window_minutes, None)

        task.add_done_callback(finished)

    async def movers(self, window_minutes: int = 15, *, force_refresh: bool = False) -> dict[str, Any]:
        if window_minutes not in movers_engine.WINDOWS_MINUTES:
            raise ValueError("INVALID_MOVERS_WINDOW")
        now = self.clock()
        cache_key = f"market:movers:computed:v1:{window_minutes}"
        cached = await self.cache.get_json(cache_key)
        if not force_refresh and isinstance(cached, dict):
            try:
                age = now - float(cached.get("updated_epoch", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                age = MOVERS_TTL + 1
            if 0 <= age < MOVERS_TTL:
                return cached
            # Keep the last computed rankings visible while the next snapshot is built.
            self._schedule_movers_refresh(window_minutes)
            stale = dict(cached)
            stale["stale"] = True
            stale["stale_seconds"] = max(0.0, age)
            return stale
        async with self._movers_locks[window_minutes]:
            cached = await self.cache.get_json(cache_key)
            if not force_refresh and isinstance(cached, dict) and now - float(cached.get("updated_epoch", 0) or 0) < MOVERS_TTL:
                return cached
            quotes = await self._quotes_map()
            coverage_status = await self.cache.get_json(MOVERS_COVERAGE_KEY)
            coverage_status = coverage_status if isinstance(coverage_status, dict) else {}
            coverage = _finite(coverage_status.get("collected_minutes")) or 0.0
            changes: dict[str, dict[str, float]] = {}
            sorted_quotes = sorted(quotes.items(), key=lambda pair: _quote_volume(pair[1]), reverse=True)
            candidates = [(s, q) for s, q in sorted_quotes if s.endswith("USDT") and _quote_volume(q) >= movers_engine.MIN_QUOTE_VOLUME][:MOVERS_SYMBOL_LIMIT]

            history_semaphore = asyncio.Semaphore(32)
            async def history_for(symbol: str, quote: dict[str, Any]):
                try:
                    async with history_semaphore:
                        history = await self.cache.get_timed_list_json(f"market:price-history:v1:{symbol}", limit=300, retention_seconds=3900)
                    row = movers_engine.window_changes(history, now=now, minutes=window_minutes, latest_quote=quote)
                    return symbol, row
                except Exception:
                    return symbol, None
            rows = await asyncio.gather(*(history_for(symbol, dict(quote)) for symbol, quote in candidates))
            for symbol, row in rows:
                if row is not None:
                    changes[symbol] = row
            ranked = movers_engine.rank_candidates(changes, window_minutes)
            ready = movers_engine.coverage_ready(coverage, window_minutes) and bool(ranked["eligible_count"])
            # Confirm volume ratio and aggressive buying on shortlisted rows only (bounded REST load).
            shortlist = []
            seen = set()
            for group in (ranked["gainers"], ranked["losers"], ranked["flat_volume"]):
                for row in group:
                    symbol = row["symbol"]
                    if symbol not in seen:
                        seen.add(symbol)
                        shortlist.append(row)
                        # Exact candle confirmation is supplementary; cap it to one
                        # bounded batch and keep estimated history for the rest.
                        if len(shortlist) >= 4:
                            break
                if len(shortlist) >= 4:
                    break
            semaphore = asyncio.Semaphore(4)
            async def enrich(row: dict[str, Any]):
                symbol = row["symbol"]
                try:
                    async with semaphore:
                        raw = await asyncio.wait_for(self.rest.fetch_klines(symbol, "5m", 120), timeout=5.0)
                    candles = flow_engine.parse_candles(raw)
                    stats = flow_engine.window_stats(candles, window_minutes, now)
                except Exception as exc:
                    logger.debug("Movers: volumen exacto no disponible para %s: %s", symbol, exc)
                    stats = None
                exact = bool(stats and stats.get("volume_ratio") is not None)
                ratio = stats.get("volume_ratio") if exact else row.get("vol_est")
                result = {key: row[key] for key in ("symbol", "price", "change_pct", "qvol") if key in row}
                result["qvol_24h"] = result.pop("qvol", None)
                result["volume_ratio"] = ratio
                result["volume_exact"] = exact
                result["buy_pct"] = stats.get("buy_pct") if stats else None
                return result
            enriched_rows = await asyncio.gather(*(enrich(row) for row in shortlist))
            by_symbol = {row["symbol"]: row for row in enriched_rows}
            enriched_gainers = [by_symbol.get(row["symbol"], {**row, "volume_ratio": row.get("vol_est"), "volume_exact": False, "buy_pct": None}) for row in ranked["gainers"]]
            enriched_losers = [by_symbol.get(row["symbol"], {**row, "volume_ratio": row.get("vol_est"), "volume_exact": False, "buy_pct": None}) for row in ranked["losers"]]
            enriched_flat = [by_symbol.get(row["symbol"], {**row, "volume_ratio": row.get("vol_est"), "volume_exact": False, "buy_pct": None}) for row in ranked["flat_volume"]]
            # Reapply original flat-volume threshold to exact confirmation when available.
            enriched_flat = [row for row in enriched_flat if (row.get("volume_ratio") or 0) >= movers_engine.FLAT_MIN_RATIO and abs(row.get("change_pct", 0)) < movers_engine.FLAT_MAX_MOVE_PCT][:3]
            output = {
                "window": window_minutes, "status": "ok" if ready else "collecting", "ready": ready,
                "collected_minutes": round(coverage, 3), "required_minutes": window_minutes * movers_engine.MIN_COVERAGE,
                "breadth": ranked["breadth"], "gainers": enriched_gainers, "losers": enriched_losers,
                "flat_volume": enriched_flat, "btc": ranked["btc"], "eligible_count": ranked["eligible_count"],
                "message": None if ready else "Acumulando historial del feed; todavía no hay cobertura suficiente para esta ventana.",
                "source": "binance-futures-live-price-history", "updated_at": utc_iso(now), "updated_epoch": now,
            }
            await self.cache.set_json(cache_key, output, ttl_seconds=180)
            return output

    async def overview(self, *, signal_limit: int = 300) -> dict[str, Any]:
        now = self.clock()
        quotes = await self._quotes_map()
        feed = await self.cache.get_json(FEED_STATUS_KEY)
        feed = feed if isinstance(feed, dict) else {"status": "unavailable", "connected": False}
        sorted_quotes = sorted(
            ((symbol, quote) for symbol, quote in quotes.items() if isinstance(quote, dict)),
            key=lambda pair: _finite(pair[1].get("change_pct")) or 0.0, reverse=True,
        )
        btc = quotes.get("BTCUSDT")
        signals = await asyncio.to_thread(self.repository.list_signals, limit=signal_limit)
        statuses: dict[str, int] = {"PENDING": 0, "TOUCHED": 0, "CANCELLED": 0, "CLOSED": 0, "OTHER": 0}
        for row in signals:
            lifecycle = row.get("lifecycle") if isinstance(row.get("lifecycle"), Mapping) else {}
            status = str(row.get("status") or lifecycle.get("status") or "OTHER").upper()
            statuses[status if status in statuses else "OTHER"] += 1
        sections = {
            "radar": await self.cache.get_json(RADAR_KEY),
            "movers_15m": await self.cache.get_json("market:movers:computed:v1:15"),
            "liquidations_15m": await self.cache.get_json("market:liquidations:summary:v1:15"),
            "regime": await self.cache.get_json(REGIME_KEY),
        }
        return {
            "status": "ok" if quotes and feed.get("status") == "healthy" else ("partial" if quotes else "degraded"),
            "updated_at": utc_iso(now),
            "market": {
                "status": feed.get("status", "unavailable"), "connected": bool(feed.get("connected")),
                "updated_at": feed.get("last_price_update_at"), "asset_count": len(quotes),
                "btc": btc,
                "gainers": [{"symbol": s, "price": q.get("price"), "change_pct": q.get("change_pct"), "volume_24h": q.get("volume_24h")} for s, q in sorted_quotes[:5]],
                "losers": [{"symbol": s, "price": q.get("price"), "change_pct": q.get("change_pct"), "volume_24h": q.get("volume_24h")} for s, q in sorted(sorted_quotes, key=lambda pair: _finite(pair[1].get("change_pct")) if _finite(pair[1].get("change_pct")) is not None else 0)[:5]],
            },
            "signals": {"scanned_rows": len(signals), "status_counts": statuses},
            "radar": sections["radar"], "movers": sections["movers_15m"],
            "liquidations": sections["liquidations_15m"], "regime": sections["regime"],
            "source": "ftrade-shared-market-cache",
        }

    async def _quotes_map(self) -> dict[str, dict[str, Any]]:
        cached = await self.cache.get_json(QUOTES_KEY)
        if isinstance(cached, dict) and cached:
            return {str(symbol).upper(): dict(row) for symbol, row in cached.items() if isinstance(row, dict)}
        try:
            raw = await self.rest.fetch_24h_tickers()
        except Exception as exc:
            logger.debug("No se pudo obtener universo de tickers para radar/movers: %s", exc)
            return {}
        result: dict[str, dict[str, Any]] = {}
        for row in raw:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol.endswith("USDT") or "_" in symbol:
                continue
            price = _finite(row.get("lastPrice", row.get("price")))
            if not price or price <= 0:
                continue
            result[symbol] = {
                "symbol": symbol, "price": price,
                "change_pct": _finite(row.get("priceChangePercent")),
                "volume_24h": _quote_volume(row),
                "high_24h": _finite(row.get("highPrice")), "low_24h": _finite(row.get("lowPrice")),
            }
        if result:
            await self.cache.set_json(QUOTES_KEY, result, ttl_seconds=90)
        return result


class AnalyticsIntelligenceUnavailable(RuntimeError):
    pass
