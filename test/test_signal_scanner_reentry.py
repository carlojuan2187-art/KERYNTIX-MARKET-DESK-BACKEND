from datetime import datetime, timedelta, timezone

from worker.signal_scanner import _fresh_sweep_after_last_sl, _latest_sl_by_symbol_direction


def test_latest_sl_is_tracked_per_symbol_and_direction():
    first = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    later = first + timedelta(minutes=15)
    rows = [
        {"symbol": "BTCUSDT", "direction": "LONG", "result": "SL", "closed_at": first},
        {"symbol": "BTCUSDT", "direction": "LONG", "result": "TP1", "closed_at": later},
        {"symbol": "BTCUSDT", "direction": "SHORT", "result": "SL", "closed_at": later},
    ]
    latest = _latest_sl_by_symbol_direction(rows)
    assert latest[("BTCUSDT", "LONG")] == first
    assert latest[("BTCUSDT", "SHORT")] == later


def test_same_side_reentry_requires_a_newer_liquidity_sweep():
    stopped_at = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    latest = {("BTCUSDT", "LONG"): stopped_at}
    assert not _fresh_sweep_after_last_sl(
        {"symbol": "BTCUSDT", "direction": "LONG", "liquidity_sweep_at": stopped_at}, latest
    )
    assert _fresh_sweep_after_last_sl(
        {"symbol": "BTCUSDT", "direction": "LONG", "liquidity_sweep_at": stopped_at + timedelta(minutes=1)}, latest
    )


def test_reentry_on_other_symbol_or_direction_is_not_blocked():
    stopped_at = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    latest = {("BTCUSDT", "LONG"): stopped_at}
    candidate_time = stopped_at - timedelta(minutes=2)
    assert _fresh_sweep_after_last_sl(
        {"symbol": "ETHUSDT", "direction": "LONG", "liquidity_sweep_at": candidate_time}, latest
    )
    assert _fresh_sweep_after_last_sl(
        {"symbol": "BTCUSDT", "direction": "SHORT", "liquidity_sweep_at": candidate_time}, latest
    )


def test_missing_sweep_time_fails_closed_only_for_same_side_after_sl():
    stopped_at = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    latest = {("BTCUSDT", "LONG"): stopped_at}
    assert not _fresh_sweep_after_last_sl(
        {"symbol": "BTCUSDT", "direction": "LONG"}, latest
    )


def test_breakout_reset_stop_does_not_block_liquidity_sweep_reentry():
    lsr_stopped_at = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    breakout_stopped_at = lsr_stopped_at + timedelta(minutes=30)
    rows = [
        {
            "symbol": "BTCUSDT", "direction": "LONG", "result": "SL",
            "strategy_name": "liquidity_sweep_reversal", "closed_at": lsr_stopped_at,
        },
        {
            "symbol": "BTCUSDT", "direction": "LONG", "result": "SL",
            "strategy_name": "breakout_reset", "closed_at": breakout_stopped_at,
        },
    ]
    latest = _latest_sl_by_symbol_direction(rows)
    assert latest[("BTCUSDT", "LONG")] == lsr_stopped_at
