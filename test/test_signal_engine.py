from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from api.cache import InMemoryJsonCache
from api.repositories.signals import InMemorySignalRepository
from api.repositories.sweeps import InMemorySweepStore
from domain.signals import adapt_bot_signal
from api.services.signals import SignalService
from engine.liquidity_strategy import mtf_strategies_by_plan
import pandas as pd
from engine.prepublication import validate_prepublication_setup
from worker import signal_scanner
from worker.signal_scanner import SignalScanner, _build_signal_document, _frame_from_klines


def _candidate(now: datetime) -> dict[str, Any]:
    return {
        "direction": "LONG",
        "entry_price": 99.5,
        "stop_loss": 98.0,
        "take_profits": [104.0, 105.5],
        "targets": [
            {"price": 104.0, "rr": 3.0, "touches": 2, "source": "15M"},
            {"price": 105.5, "rr": 4.0, "touches": 1, "source": "1H"},
        ],
        "timeframes": ["15M", "5M"],
        "entry_zone": {"low": 99.4, "high": 99.6},
        "score": 87.0,
        "raw_score": 87.0,
        "normalized_score": 87.0,
        "components": [{"name": "recovery", "points": 12.0, "value": 0.8, "passed": True}],
        "raw_components": [{"name": "recovery", "points": 12.0, "value": 0.8, "passed": True}],
        "normalized_components": [{"name": "recovery", "points": 12.0, "value": 0.8, "passed": True}],
        "setup_group": "premium",
        "delivery_plan": "premium",
        "filter_profile": "plus",
        "score_profile": "premium",
        "score_calibration": "v4_2_liquidity_sweep_operational_receptive",
        "atr_pct": 0.01,
        "atr_value": 1.0,
        "risk_atr": 1.5,
        "target_model": "structural_liquidity_v1",
        "send_mode": "pending_entry",
        "setup_stage": "confirmed_waiting_pullback",
        "entry_model": "liquidity_sweep_reversal_predictive_pullback_v4",
        "entry_model_price": 99.8,
        "entry_sent_price": 100.2,
        "execution_timeframe": "5M",
        "setup_confirmed_at": now - timedelta(seconds=30),
        "preentry_distance_atr": 0.5,
        "bars_after_sweep": 1,
        "sweep_to_market_atr": 0.9,
        "tp1_rr": 3.0,
        "liquidity_zone": 99.0,
        "liquidity_sweep_distance_atr": 0.2,
        "liquidity_sweep_extreme_price": 98.9,
        "higher_tf_context": {"liquidity_zone": 99.0, "sweep_distance_atr": 0.2},
        "strategy_runtime": {"post_fill_invalidation": {"minutes": 35, "min_tp1_progress_pct": 0.15, "reason": "liquidity_no_followthrough"}},
    }


def _kline(ts: datetime, close: float = 100.0) -> list[Any]:
    ms = int(ts.timestamp() * 1000)
    close_ms = ms + 59_999
    return [ms, str(close), str(close + 0.2), str(close - 0.2), str(close), "1000", close_ms, "100000", 10, "500", "50000", "0"]


class FakeMarketClient:
    def __init__(self, now: datetime) -> None:
        self.now = now

    async def fetch_24h_tickers(self):
        return [
            {"symbol": "BTCUSDT", "quoteVolume": "90000000"},
            {"symbol": "ETHUSDT", "quoteVolume": "20000000"},
            {"symbol": "BADUSDC", "quoteVolume": "999999999"},
        ]

    async def fetch_klines(self, symbol: str, interval: str, limit: int, *, start_time_ms=None, end_time_ms=None):
        if interval == "1m":
            return []
        return [_kline(self.now - timedelta(minutes=5), 100.0)]

    async def fetch_symbol_price(self, symbol: str):
        return 100.2


@pytest.mark.parametrize("direction,price,ok", [
    ("LONG", 100.2, True),
    ("LONG", 99.5, False),
    ("LONG", 104.0, False),
    ("SHORT", 98.8, True),
    ("SHORT", 99.5, False),
    ("SHORT", 95.0, False),
])
def test_prepublication_gate_handles_both_directions(direction, price, ok):
    signal = _candidate(datetime.now(timezone.utc))
    if direction == "SHORT":
        signal.update({"direction": "SHORT", "entry_price": 99.5,
                       "entry_zone": {"low": 99.4, "high": 99.6},
                       "stop_loss": 101.0, "take_profits": [96.0, 95.0]})
    accepted, _reason = validate_prepublication_setup(signal, current_price=price, candles=[])
    assert accepted is ok


def test_prepublication_gate_rejects_entry_touch_candle():
    signal = _candidate(datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)
    accepted, reason = validate_prepublication_setup(
        signal,
        current_price=100.2,
        candles=[{"low": 99.5, "high": 100.1, "open_time": now - timedelta(minutes=1), "close_time": now}],
    )
    assert not accepted
    assert reason == "ENTRY_ALREADY_TOUCHED"


def test_frame_from_binance_rows_keeps_required_ohlcv_and_utc_close_time():
    now = datetime.now(timezone.utc)
    frame = _frame_from_klines([_kline(now, 101.25)])
    assert list(frame.columns) == ["open", "high", "low", "close", "volume", "close_time"]
    assert frame.iloc[0]["close"] == 101.25
    assert str(frame.iloc[0]["close_time"].tz) == "UTC"


def test_signal_document_preserves_strategy_metadata_for_api():
    now = datetime.now(timezone.utc)
    source = _candidate(now)
    document = _build_signal_document(source, symbol="btcusdt", visibility="premium", current_price=100.2, now=now)
    assert document["symbol"] == "BTCUSDT"
    assert document["status"] == "PENDING"
    assert document["entry_zone"] == {"low": 99.4, "high": 99.6}
    assert document["higher_tf_context"] == source["higher_tf_context"]
    assert document["preentry_distance_atr"] == 0.5
    canonical = adapt_bot_signal(document)
    assert canonical.strategy.filter_profile == "plus"
    assert canonical.strategy.liquidity_zone == 99.0
    assert canonical.scoring.components[0].name == "recovery"
    assert canonical.targets[1].source == "1H"


@pytest.mark.asyncio
async def test_scan_cycle_persists_one_real_candidate_and_skips_low_volume_symbols(monkeypatch):
    now = datetime.now(timezone.utc)
    candidate = _candidate(now)
    def fake_strategy(*args, **kwargs):
        return {"premium": dict(candidate)}
    monkeypatch.setattr(signal_scanner, "mtf_strategies_by_plan", fake_strategy)

    repository = InMemorySignalRepository()
    cache = InMemoryJsonCache()
    scanner = SignalScanner(
        FakeMarketClient(now), repository, InMemorySweepStore(), cache=cache,
        min_quote_volume=50_000_000, concurrency=2, clock=lambda: now,
    )
    result = await scanner.scan_once()
    assert result["status"] == "ok"
    assert result["symbols_seen"] == 1
    assert result["signals_created"] == 1
    saved = repository.latest_by_symbol("BTCUSDT")
    assert saved is not None
    assert saved["status"] == "PENDING"
    assert saved["strategy_runtime"] == candidate["strategy_runtime"]
    projected = SignalService(repository).list_visible(limit=10)
    assert len(projected) == 1
    assert projected[0].symbol == "BTCUSDT"
    assert projected[0].tp1 == 104.0 and projected[0].tp2 == 105.5
    assert projected[0].strategy.filter_profile == "plus"
    assert cache.published and cache.published[0][0] == "signals:new:v1"


@pytest.mark.asyncio
async def test_scan_cycle_does_not_create_duplicate_pending_signal(monkeypatch):
    now = datetime.now(timezone.utc)
    candidate = _candidate(now)
    monkeypatch.setattr(signal_scanner, "mtf_strategies_by_plan", lambda *args, **kwargs: {"premium": dict(candidate)})
    repository = InMemorySignalRepository()
    scanner = SignalScanner(FakeMarketClient(now), repository, InMemorySweepStore(), min_quote_volume=50_000_000, clock=lambda: now)
    first = await scanner.scan_once()
    second = await scanner.scan_once()
    assert first["signals_created"] == 1
    assert second["signals_created"] == 0
    assert len(repository.list_signals()) == 1


def test_real_vendored_strategy_never_generates_from_insufficient_history():
    small = pd.DataFrame({
        "open": [100.0] * 8,
        "high": [101.0] * 8,
        "low": [99.0] * 8,
        "close": [100.0] * 8,
        "volume": [1000.0] * 8,
        "close_time": pd.date_range(end=datetime.now(timezone.utc), periods=8, freq="5min"),
    })
    diagnostics: dict[str, int] = {}
    result = mtf_strategies_by_plan(small, small, small, reference_market_price=100.0, debug_counts=diagnostics)
    assert result == {}
    assert any("insufficient_history" in reason for reason in diagnostics)
