"""Normalize HADES PLUS strategy candidates for KERYNTIX's canonical signal pipeline."""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any, Mapping

import pandas as pd

from engine import breakout_reset_strategy, liquidity_sweep_strategy


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and isfinite(number) else None


def _to_utc(value: Any) -> datetime | None:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _closed_row(frame: pd.DataFrame) -> pd.Series | None:
    if frame is None or frame.empty:
        return None
    if "close_time" not in frame.columns:
        return frame.iloc[-1]
    times = pd.to_datetime(frame["close_time"], utc=True, errors="coerce")
    now = pd.Timestamp.now(tz="UTC")
    closed = frame.loc[times.notna() & (times <= now)]
    return closed.iloc[-1] if not closed.empty else (frame.iloc[-2] if len(frame) > 1 else frame.iloc[-1])


def _entry_zone(entry_price: float, stop_loss: float) -> dict[str, float]:
    """Mirror HADES signals.calculate_entry_zone() defaults and risk fraction."""
    risk_pct = abs(float(entry_price) - float(stop_loss)) / max(abs(float(entry_price)), 1e-9)
    fraction = float(breakout_reset_strategy.ENTRY_ZONE_RISK_FRACTION)
    min_pct = float(breakout_reset_strategy.ENTRY_ZONE_MIN_PCT)
    max_pct = float(breakout_reset_strategy.ENTRY_ZONE_MAX_PCT)
    pct = max(min_pct, min(max_pct, risk_pct * fraction))
    low = breakout_reset_strategy._round_price_dynamic(float(entry_price) * (1.0 - pct))
    high = breakout_reset_strategy._round_price_dynamic(float(entry_price) * (1.0 + pct))
    return {"low": min(low, high), "high": max(low, high)}


def prepare_candidate(
    candidate: Mapping[str, Any],
    *,
    df_1h: pd.DataFrame,
    df_15m: pd.DataFrame,
    df_5m: pd.DataFrame,
) -> dict[str, Any] | None:
    """Attach KERYNTIX-required metadata without changing strategy levels or score."""
    payload = dict(candidate or {})
    strategy_name = str(payload.get("strategy_name") or "").strip().lower()
    if strategy_name not in {"breakout_reset", "liquidity_sweep_reversal"}:
        return None

    entry = _finite_positive(payload.get("entry_price"))
    stop = _finite_positive(payload.get("stop_loss"))
    targets = payload.get("take_profits") or []
    if entry is None or stop is None or not targets:
        return None
    try:
        tp1 = float(targets[0])
    except (TypeError, ValueError):
        return None
    direction = str(payload.get("direction") or "").upper()
    if direction not in {"LONG", "SHORT"}:
        return None
    if direction == "LONG" and not (stop < entry < tp1):
        return None
    if direction == "SHORT" and not (tp1 < entry < stop):
        return None

    # HADES signal construction calculated the entry zone from the risk distance,
    # not from the strategy's visual reset-zone boundaries.
    payload["entry_zone"] = _entry_zone(entry, stop)
    payload["setup_group"] = "plus"
    payload["score_profile"] = "plus"
    payload["filter_profile"] = "plus"

    if strategy_name == "breakout_reset":
        # Breakout Reset evaluates the most recent closed 5m candle as setup and
        # keeps the final row as the live-price row.
        setup_frame = df_5m.iloc[:-1] if df_5m is not None and len(df_5m) > 1 else df_5m
        row = _closed_row(setup_frame) if setup_frame is not None else None
        if row is None:
            return None
        payload.setdefault("timeframes", ["5M"])
        payload.setdefault("execution_timeframe", "5M")
    else:
        closed_15m = liquidity_sweep_strategy._closed_15m_frame(df_15m)
        enriched = liquidity_sweep_strategy.add_indicators(closed_15m) if not closed_15m.empty else closed_15m
        row = _closed_row(closed_15m)
        if row is None:
            return None
        payload.setdefault("timeframes", ["15M", "5M"])
        payload.setdefault("execution_timeframe", "15M")
        if "atr_value" not in payload and not enriched.empty:
            payload["atr_value"] = _finite_positive(enriched.iloc[-1].get("atr"))

    confirmed_at = _to_utc(row.get("close_time"))
    if confirmed_at is None:
        return None
    payload["setup_confirmed_at"] = confirmed_at
    payload["entry_price"] = entry
    payload["stop_loss"] = stop
    payload["take_profits"] = [float(value) for value in targets]
    payload["entry_model_price"] = _finite_positive(payload.get("entry_model_price")) or entry
    payload["entry_sent_price"] = _finite_positive(payload.get("entry_sent_price"))
    payload["atr_pct"] = payload.get("atr_pct")
    return payload
