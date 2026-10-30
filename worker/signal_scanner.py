"""Live signal-generation worker for FTrade Web.

This worker ports the current bot's scan orchestration around the same strategy
function. It does not send Telegram notifications; it persists a canonical signal
for the web API after the bot's last-moment anti-late checks pass.
"""
from __future__ import annotations

from api.logging_config import configure_logging

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

import pandas as pd

from api.cache import JsonCache, cache_from_environment
from api.repositories.signals import SignalRepository, repository_from_environment
from api.repositories.sweeps import (
    RETENTION_MINUTES, SweepStore, sweep_store_from_environment,
)
from domain.signals import adapt_bot_signal
from engine.liquidity_strategy import mtf_strategies_by_plan
from engine.prepublication import validate_prepublication_setup
from worker.binance_rest import BinanceRestClient

logger = logging.getLogger("ftrade.signal_engine")

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]
OHLCV_COLUMNS = ["open", "high", "low", "close", "volume", "close_time"]

MIN_QUOTE_VOLUME = max(0.0, float(os.getenv("MIN_QUOTE_VOLUME", "50000000")))
SCAN_INTERVAL_SECONDS = max(10, int(os.getenv("SCAN_INTERVAL_SECONDS", "60")))
SCAN_CONCURRENCY = max(1, min(8, int(os.getenv("SCAN_CONCURRENCY", "4"))))
DEDUP_MINUTES = max(1, int(os.getenv("DEDUP_MINUTES", "10")))
ENTRY_WINDOW_MINUTES = max(5, int(os.getenv("ENTRY_WINDOW_MINUTES", "15")))
SIGNAL_STATUS_KEY = "signal-engine:status:v1"


def _utc_aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _frame_from_klines(rows: list[list[Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    frame = pd.DataFrame(rows, columns=KLINE_COLUMNS[: len(rows[0])])
    required = {"open", "high", "low", "close", "volume", "close_time"}
    if not required.issubset(frame.columns):
        return pd.DataFrame(columns=OHLCV_COLUMNS)
    frame["close_time"] = pd.to_datetime(frame["close_time"], unit="ms", utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[OHLCV_COLUMNS].dropna(subset=["open", "high", "low", "close", "volume", "close_time"])
    return frame.reset_index(drop=True)


def _observation_key(symbol: str, profile: str, direction: str) -> str:
    return f"{symbol}:{profile}:{direction}"


def _make_observation(
    symbol: str, profile: str, direction: str, sweep: Mapping[str, Any], level: float, candle_time: Any,
    *, now: datetime,
) -> dict[str, Any] | None:
    event_at = _utc_aware(candle_time)
    if event_at is None or event_at > now + timedelta(seconds=5) or event_at < now - timedelta(minutes=RETENTION_MINUTES):
        return None
    return {
        "_id": _observation_key(symbol, profile, direction),
        "symbol": symbol,
        "filter_profile": profile,
        "direction": direction,
        "event_at": event_at,
        "expires_at": event_at + timedelta(minutes=RETENTION_MINUTES),
        "level": float(level),
        "extreme_price": float(sweep["extreme_price"]),
        "sweep_distance_atr": float(sweep.get("sweep_distance_atr", 0)),
        "wick_ratio": float(sweep.get("wick_ratio", 0)),
        "sweep_mode": sweep.get("sweep_mode", "unknown"),
    }


def estimate_minutes_to_entry(entry_zone: Mapping[str, float], timeframes: list[str], current_price: float) -> dict[str, int]:
    """Mirror app.signals.estimate_minutes_to_entry for the bot's 5m signal profile."""
    zone_mid = (float(entry_zone["low"]) + float(entry_zone["high"])) / 2
    if float(entry_zone["low"]) <= current_price <= float(entry_zone["high"]):
        return {"min": 1, "max": 5}
    distance_pct = abs(current_price - zone_mid) / max(abs(current_price), 1e-12)
    if "5M" in [str(tf).upper() for tf in timeframes]:
        speed, base_tf = 0.004, 5
    elif "15M" in [str(tf).upper() for tf in timeframes]:
        speed, base_tf = 0.0025, 15
    else:
        speed, base_tf = 0.0015, 15
    candles_needed = max(1, distance_pct / speed)
    minutes = candles_needed * base_tf
    return {"min": max(1, int(minutes * 0.6)), "max": max(1, int(minutes * 1.4))}


def _api_candles_to_events(rows: list[list[Any]], *, start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
    start = _utc_aware(start_at)
    end = _utc_aware(end_at)
    if start is None or end is None:
        return []
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            open_at = datetime.fromtimestamp(int(row[0]) / 1000.0, tz=timezone.utc)
            close_at = datetime.fromtimestamp(int(row[6]) / 1000.0, tz=timezone.utc)
            if open_at < start or close_at > end:
                continue
            events.append({
                "open_time": open_at,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "close_time": close_at,
            })
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
    return events


def _build_signal_document(
    candidate: Mapping[str, Any], *, symbol: str, visibility: str, current_price: float,
    now: datetime, entry_window_minutes: int = ENTRY_WINDOW_MINUTES,
) -> dict[str, Any]:
    entry_price = float(candidate["entry_price"])
    stop_loss = float(candidate["stop_loss"])
    take_profits = [float(target) for target in candidate.get("take_profits", [])]
    if not take_profits or stop_loss <= 0:
        raise ValueError("La estrategia produjo una señal sin SL/TP válidos")
    zone = candidate.get("entry_zone")
    if not isinstance(zone, Mapping) or zone.get("low") is None or zone.get("high") is None:
        raise ValueError("La estrategia produjo una señal sin zona de entrada explícita")
    low, high = sorted((float(zone["low"]), float(zone["high"])))
    entry_zone = {"low": low, "high": high}
    expires_at = now + timedelta(minutes=max(5, int(entry_window_minutes)))
    timeframes = [str(tf).upper() for tf in candidate.get("timeframes", ["15M", "5M"])]

    # Keep the strategy payload's full metadata. The API canonical adapter maps the
    # known fields and preserves additional ones under strategy metadata.
    document: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "schema_version": "1.0",
        "symbol": symbol.upper(),
        "direction": str(candidate["direction"]).upper(),
        "visibility": visibility.lower(),
        "entry_price": entry_price,
        "entry_zone": entry_zone,
        "stop_loss": stop_loss,
        "take_profits": take_profits,
        "targets": [dict(item) for item in (candidate.get("targets") or []) if isinstance(item, Mapping)],
        "timeframes": timeframes,
        "score": float(candidate["score"]) if candidate.get("score") is not None else None,
        "components": list(candidate.get("components") or []),
        "raw_score": candidate.get("raw_score"),
        "normalized_score": candidate.get("normalized_score"),
        "raw_components": list(candidate.get("raw_components") or candidate.get("components") or []),
        "normalized_components": list(candidate.get("normalized_components") or candidate.get("components") or []),
        "score_profile": candidate.get("score_profile"),
        "score_calibration": candidate.get("score_calibration"),
        "created_at": now,
        "status": "PENDING",
        "pending_at": now,
        "status_updated_at": now,
        "valid_until": expires_at,
        "telegram_valid_until": expires_at,
        "entry_expires_at": expires_at,
        "display_until": expires_at,
        "estimated_entry_minutes": estimate_minutes_to_entry(entry_zone, timeframes, float(current_price)),
        "touched_at": None,
        "touch_price": None,
        "cancelled_at": None,
        "cancel_reason": None,
        "outcome": {},
    }
    # Copy every non-core strategy field, including new metadata introduced by the
    # strategy. This intentionally avoids the Telegram bot's former allow-list loss.
    core = {
        "direction", "entry_price", "stop_loss", "take_profits", "targets", "score",
        "components", "raw_score", "normalized_score", "raw_components", "normalized_components",
        "score_profile", "score_calibration", "timeframes", "entry_zone", "symbol",
    }
    for key, value in candidate.items():
        if key not in core:
            document[key] = value
    document["entry_sent_price"] = float(current_price)
    document["setup_confirmed_at"] = _utc_aware(candidate.get("setup_confirmed_at")) or now
    document["strategy_runtime"] = candidate.get("strategy_runtime")
    # Validate before persistence and store canonical nested sections alongside flat
    # legacy aliases. This keeps existing API projections/indexes working while the
    # full schema becomes the durable contract.
    canonical = adapt_bot_signal(document, include_trade_metrics=False)
    canonical_data = canonical.model_dump(mode="python")
    for section in ("scoring", "strategy", "lifecycle", "outcome", "schema_version", "targets"):
        document[section] = canonical_data[section]
    return document


class SignalScanner:
    """Runs one scan cycle or continuously; all external dependencies are injected."""

    def __init__(
        self,
        market_client: Any,
        repository: SignalRepository,
        sweep_store: SweepStore,
        *,
        cache: JsonCache | None = None,
        min_quote_volume: float = MIN_QUOTE_VOLUME,
        concurrency: int = SCAN_CONCURRENCY,
        dedupe_minutes: int = DEDUP_MINUTES,
        entry_window_minutes: int = ENTRY_WINDOW_MINUTES,
        interval_seconds: int = SCAN_INTERVAL_SECONDS,
        request_delay_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.market_client = market_client
        self.repository = repository
        self.sweep_store = sweep_store
        self.cache = cache
        self.min_quote_volume = max(0.0, float(min_quote_volume))
        self.concurrency = max(1, min(8, int(concurrency)))
        self.dedupe_minutes = max(1, int(dedupe_minutes))
        self.entry_window_minutes = max(5, int(entry_window_minutes))
        self.interval_seconds = max(10, int(interval_seconds))
        self.request_delay_seconds = max(0.0, float(os.getenv("BINANCE_REQUEST_DELAY", "0.2") if request_delay_seconds is None else request_delay_seconds))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = asyncio.Event()
        self._rate_lock = asyncio.Lock()
        self._last_request_started = 0.0

    async def _throttled(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        # Preserve the bot scanner's shared 0.2s request pacing and avoid bursts
        # when many symbols are analyzed concurrently.
        async with self._rate_lock:
            now = time.monotonic()
            wait = self.request_delay_seconds - (now - self._last_request_started)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_started = time.monotonic()
        return await method(*args, **kwargs)

    async def scan_once(self) -> dict[str, Any]:
        started = time.monotonic()
        now = _utc_aware(self.clock()) or datetime.now(timezone.utc)
        try:
            tickers = await self._throttled(self.market_client.fetch_24h_tickers)
        except Exception as exc:
            await self._write_status({"status": "degraded", "last_scan_at": now.isoformat(), "error": "BINANCE_TICKERS_UNAVAILABLE"})
            logger.warning("No se pudo obtener el listado de contratos: %s", exc)
            return {"status": "degraded", "symbols_seen": 0, "signals_created": 0, "scan_errors": 1}

        symbols: list[str] = []
        for row in tickers:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol.endswith("USDT") or "_" in symbol:
                continue
            try:
                volume = float(row.get("quoteVolume", 0))
            except (TypeError, ValueError):
                continue
            if volume >= self.min_quote_volume:
                symbols.append(symbol)
        # Keep deterministic order for repeatable ranking in ties.
        symbols = sorted(set(symbols))
        try:
            active_sweeps = self.sweep_store.load_active(now=now)
        except Exception as exc:
            active_sweeps = {}
            logger.warning("No se pudieron cargar barridos activos; el ciclo sigue sin memoria de sweep: %s", exc)

        semaphore = asyncio.Semaphore(self.concurrency)
        observations: list[dict[str, Any]] = []
        candidates: list[tuple[str, dict[str, Any]]] = []
        symbol_errors = 0
        rejects: dict[str, int] = {}

        async def analyze(symbol: str) -> tuple[str, dict[str, dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
            async with semaphore:
                # Keep one candidate's mutable diagnostics and observations local.
                found_observations: list[dict[str, Any]] = []
                debug_counts: dict[str, int] = {}
                one_hour, fifteen, five = await asyncio.gather(
                    self._throttled(self.market_client.fetch_klines, symbol, "1h", 200),
                    self._throttled(self.market_client.fetch_klines, symbol, "15m", 200),
                    self._throttled(self.market_client.fetch_klines, symbol, "5m", 200),
                )
                df_1h = _frame_from_klines(one_hour)
                df_15m = _frame_from_klines(fifteen)
                df_5m = _frame_from_klines(five)
                if df_5m.empty:
                    return symbol, {}, {"empty_5m_history": 1}, found_observations
                reference_price = float(df_5m.iloc[-1]["close"])
                observed = {
                    (profile, direction): active_sweeps.get(_observation_key(symbol, profile, direction))
                    for profile in ("plus",)
                    for direction in ("LONG", "SHORT")
                }

                def on_sweep(profile: str, direction: str, sweep: Mapping[str, Any], level: float, candle_time: Any) -> None:
                    item = _make_observation(symbol, profile, direction, sweep, level, candle_time, now=now)
                    if item is not None:
                        found_observations.append(item)

                result = mtf_strategies_by_plan(
                    df_1h,
                    df_15m,
                    df_5m,
                    reference_market_price=reference_price,
                    debug_counts=debug_counts,
                    observed_sweeps=observed,
                    on_sweep=on_sweep,
                )
                return symbol, result, debug_counts, found_observations

        tasks = [asyncio.create_task(analyze(symbol)) for symbol in symbols]
        try:
            for task in asyncio.as_completed(tasks):
                try:
                    symbol, results, debug, discovered = await task
                    observations.extend(discovered)
                    for reason, count in debug.items():
                        rejects[reason] = rejects.get(reason, 0) + int(count)
                    for visibility, payload in results.items():
                        if payload:
                            candidates.append((visibility, {**payload, "symbol": symbol}))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    symbol_errors += 1
                    logger.warning("Error analizando símbolo durante el scan (%s)", type(exc).__name__)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            sweeps_saved = self.sweep_store.save_observations(observations)
        except Exception as exc:
            sweeps_saved = 0
            logger.warning("No se pudieron guardar barridos observados: %s", exc)

        candidates.sort(key=lambda pair: float(pair[1].get("score") or 0), reverse=True)
        signals_created = 0
        attempted_candidates = 0
        for visibility, candidate in candidates:
            symbol = str(candidate.get("symbol") or "").upper()
            direction = str(candidate.get("direction") or "").upper()
            if not symbol or direction not in {"LONG", "SHORT"}:
                continue
            if not candidate.get("setup_confirmed_at"):
                continue
            if float(candidate.get("stop_loss") or 0) <= 0 or not candidate.get("take_profits"):
                continue
            attempted_candidates += 1
            confirmed_at = _utc_aware(candidate.get("setup_confirmed_at"))
            if confirmed_at is None:
                continue
            current_time = _utc_aware(self.clock()) or datetime.now(timezone.utc)
            start_ms = int(confirmed_at.timestamp() * 1000)
            end_ms = int(current_time.timestamp() * 1000)
            try:
                live_price, candle_rows = await asyncio.gather(
                    self._throttled(self.market_client.fetch_symbol_price, symbol),
                    self._throttled(self.market_client.fetch_klines, symbol, "1m", 500, start_time_ms=start_ms, end_time_ms=end_ms),
                )
            except Exception as exc:
                logger.info("Señal %s omitida: mercado no disponible para última validación (%s)", symbol, type(exc).__name__)
                continue
            one_minute = _api_candles_to_events(candle_rows, start_at=confirmed_at, end_at=current_time)
            candidate["entry_sent_price"] = float(live_price)
            allowed, reason = validate_prepublication_setup(candidate, current_price=float(live_price), candles=one_minute)
            if not allowed:
                logger.debug("Candidato descartado antes de persistir: %s %s %s", symbol, direction, reason)
                continue
            try:
                document = _build_signal_document(candidate, symbol=symbol, visibility=visibility, current_price=float(live_price), now=current_time, entry_window_minutes=self.entry_window_minutes)
                created = self.repository.create_signal_if_not_duplicate(document, dedupe_minutes=self.dedupe_minutes)
            except Exception as exc:
                logger.error("No se pudo persistir señal %s %s (%s)", symbol, direction, type(exc).__name__)
                continue
            if created:
                signals_created += 1
                await self._publish_signal(document)
                logger.info("Señal web guardada %s %s score=%.2f visibility=%s", symbol, direction, float(candidate.get("score") or 0), visibility)
                # Bot behavior: one signal per profile/scan cycle.
                break

        result = {
            "status": "ok" if symbol_errors == 0 else "degraded",
            "started_at": now.isoformat(),
            "symbols_seen": len(symbols),
            "symbols_with_errors": symbol_errors,
            "candidates": len(candidates),
            "candidates_prechecked": attempted_candidates,
            "signals_created": signals_created,
            "sweeps_observed": len(observations),
            "sweeps_saved": sweeps_saved,
            "duration_seconds": round(time.monotonic() - started, 3),
            "top_rejections": dict(sorted(rejects.items(), key=lambda pair: pair[1], reverse=True)[:12]),
        }
        await self._write_status({**result, "last_scan_at": now.isoformat()})
        return result

    async def run_forever(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            began = time.monotonic()
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error crítico durante el ciclo de señales")
                await self._write_status({"status": "degraded", "error": "SCAN_CYCLE_FAILED", "last_error_at": self.clock().isoformat()})
            delay = max(0.0, self.interval_seconds - (time.monotonic() - began))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def _write_status(self, status: Mapping[str, Any]) -> None:
        if self.cache is None:
            return
        try:
            await self.cache.set_json(SIGNAL_STATUS_KEY, dict(status), ttl_seconds=3600)
        except Exception as exc:
            logger.debug("No se pudo guardar el estado del scanner: %s", type(exc).__name__)

    async def _publish_signal(self, document: Mapping[str, Any]) -> None:
        if self.cache is None:
            return
        try:
            await self.cache.publish_json("signals:new:v1", {
                "id": document.get("id"),
                "symbol": document.get("symbol"),
                "direction": document.get("direction"),
                "visibility": document.get("visibility"),
                "created_at": document.get("created_at").isoformat() if isinstance(document.get("created_at"), datetime) else document.get("created_at"),
            })
        except Exception as exc:
            logger.debug("No se pudo publicar evento de señal: %s", type(exc).__name__)


async def _run_worker() -> None:
    configure_logging()
    repository = repository_from_environment()
    repo_health = repository.health()
    if not repo_health.get("persistent") and os.getenv("SIGNAL_ENGINE_ALLOW_MEMORY", "false").lower() not in {"1", "true", "yes"}:
        raise RuntimeError("El worker necesita MongoDB persistente. Para una prueba aislada, permite memoria con SIGNAL_ENGINE_ALLOW_MEMORY=true (no compartirá datos con otra instancia de la API).")
    sweeps = sweep_store_from_environment()
    cache = cache_from_environment()
    rest = BinanceRestClient()
    scanner = SignalScanner(rest, repository, sweeps, cache=cache)
    logger.info("Worker de señales iniciado; almacenamiento=%s", repo_health.get("adapter"))
    try:
        from worker.distributed_lease import DistributedWorkerLease
        async with DistributedWorkerLease(cache, "signal-scanner"):
            await scanner.run_forever()
    finally:
        await rest.close()
        await cache.close()
        sweeps.close()
        close = getattr(repository, "close", None)
        if callable(close):
            close()


def main() -> None:
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
