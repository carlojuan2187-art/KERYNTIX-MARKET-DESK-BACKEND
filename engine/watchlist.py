"""Pure watchlist normalization and multi-timeframe conviction score.

The score logic is ported from KERYNTIX MARKET DESK bot app/watchlist.py. It is informational
only and never creates, filters or modifies trading signals.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from engine.derivatives import validate_symbol
from engine.regime import ADX_RANGE_MAX, ADX_STRONG, REGIME_DOWN, REGIME_UP, TIMEFRAME_WEIGHTS

MAX_PAIRS = 10
TIMEFRAMES = ("15m", "1h", "4h")
STRONG_SCORE = 75
MODERATE_SCORE = 55
SWEEP_WINDOW_MINUTES = 20
SWEEP_BONUS_MAX = 6
SUGGESTED_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT")


def normalize_symbol(value: Any) -> str:
    """Normalize common inputs ('avax', 'AVAX/USDT', '$avaxusdt') to AVAXUSDT."""
    raw = str(value or "").strip().upper()
    if not raw or re.search(r"\s", raw):
        raise ValueError("INVALID_SYMBOL")
    raw = re.sub(r"[/_\-:$#]", "", raw)
    base = raw[:-4] if raw.endswith("USDT") else raw
    if not base:
        raise ValueError("INVALID_SYMBOL")
    return validate_symbol(base + "USDT")


def short_name(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    return normalized[:-4]


def user_symbols(user: Mapping[str, Any] | None) -> list[str]:
    seen: list[str] = []
    raw = (user or {}).get("watchlist") or []
    if not isinstance(raw, list):
        return []
    for item in raw:
        try:
            symbol = validate_symbol(str(item))
        except ValueError:
            continue
        if symbol not in seen:
            seen.append(symbol)
    return seen[:MAX_PAIRS]


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _sign(analysis: Mapping[str, Any] | None) -> int | None:
    if not analysis:
        return None
    regime = analysis.get("regime")
    return 1 if regime == REGIME_UP else -1 if regime == REGIME_DOWN else 0


def _conviction(analysis: Mapping[str, Any] | None) -> float | None:
    sign = _sign(analysis)
    if sign is None:
        return None
    if sign == 0:
        return 0.0
    try:
        adx = float(analysis.get("adx") or 0.0)
    except (TypeError, ValueError):
        adx = 0.0
    normalized = _clamp((adx - ADX_RANGE_MAX) / (ADX_STRONG - ADX_RANGE_MAX))
    return sign * (0.5 + 0.5 * normalized)


def compute_score(analyses: Mapping[str, Mapping[str, Any] | None], sweep: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    weighted = 0.0
    total_weight = 0.0
    for timeframe in TIMEFRAMES:
        conviction = _conviction(analyses.get(timeframe))
        if conviction is None:
            continue
        weight = TIMEFRAME_WEIGHTS[timeframe]
        weighted += weight * conviction
        total_weight += weight
    if total_weight <= 0:
        return None
    value = weighted / total_weight
    lean = "LONG" if value > 0 else "SHORT" if value < 0 else "NEUTRAL"
    score = int(round(abs(value) * 100))
    if sweep and lean != "NEUTRAL" and sweep.get("direction") == lean:
        try:
            minutes = float(sweep.get("minutes", SWEEP_WINDOW_MINUTES))
        except (TypeError, ValueError):
            minutes = SWEEP_WINDOW_MINUTES
        if 0 <= minutes < SWEEP_WINDOW_MINUTES:
            score += max(1, int(round(SWEEP_BONUS_MAX * (1 - minutes / SWEEP_WINDOW_MINUTES))))
    score = min(100, score)
    strength = "fuerte" if score >= STRONG_SCORE else "moderado" if score >= MODERATE_SCORE else None
    return {
        "direction": lean if strength else "NEUTRAL",
        "lean": lean,
        "strength": strength,
        "score": score,
        "signs": {tf: _sign(analyses.get(tf)) for tf in TIMEFRAMES},
    }
