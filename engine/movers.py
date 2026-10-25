"""Pure ranking helpers for movers, following the original bot's thresholds."""
from __future__ import annotations

from typing import Any

WINDOWS_MINUTES = (5, 15, 60)
SAMPLE_SECONDS = 15.0
MIN_QUOTE_VOLUME = 20_000_000.0
TOP_N = 5
CANDIDATES_PER_SIDE = 10
FLAT_CANDIDATES = 8
MIN_LIST_MOVE_PCT = {5: 0.4, 15: 0.8, 60: 1.5}
FLAT_MAX_MOVE_PCT = 1.0
FLAT_MIN_RATIO = 3.0
MIN_COVERAGE = 0.9


def coverage_ready(coverage_minutes: float, window_minutes: int) -> bool:
    return coverage_minutes >= window_minutes * MIN_COVERAGE


def window_changes(history: list[dict[str, Any]], *, now: float, minutes: int, latest_quote: dict[str, Any]) -> dict[str, float] | None:
    """Estimate price change using actual sampled stream history, not mock data."""
    if not history:
        return None
    ordered = sorted((row for row in history if isinstance(row, dict)), key=lambda row: float(row.get("timestamp", 0)))
    try:
        latest_ts = float(ordered[-1]["timestamp"])
        latest_price = float(latest_quote.get("price", ordered[-1].get("price")))
        qvol = float(latest_quote.get("volume_24h", latest_quote.get("quote_volume", ordered[-1].get("volume_24h", 0))))
    except (TypeError, ValueError, KeyError):
        return None
    if latest_ts <= 0 or now - latest_ts > 120 or latest_price <= 0 or qvol < 0:
        return None
    target = now - minutes * 60
    slack = minutes * 60 * (1 - MIN_COVERAGE) + SAMPLE_SECONDS
    old = None
    for row in ordered:
        try:
            ts = float(row.get("timestamp", 0))
            price = float(row.get("price", 0))
            vol = float(row.get("volume_24h", 0))
        except (TypeError, ValueError):
            continue
        if ts <= target and price > 0:
            old = (ts, price, vol)
        elif ts > target:
            break
    if old is None:
        try:
            first = ordered[0]
            first_ts = float(first.get("timestamp", 0))
            first_price = float(first.get("price", 0))
            first_vol = float(first.get("volume_24h", 0))
        except (TypeError, ValueError):
            return None
        if first_ts - target <= slack and first_price > 0:
            old = (first_ts, first_price, first_vol)
    if old is None or target - old[0] > SAMPLE_SECONDS * 4 or old[1] <= 0:
        return None
    base_window = qvol * minutes / 1440.0
    volume_est = 1 + (qvol - old[2]) / base_window if base_window > 0 and old[2] > 0 else None
    return {"price": latest_price, "change_pct": (latest_price / old[1] - 1) * 100,
            "qvol": qvol, "vol_est": volume_est}


def rank_candidates(changes: dict[str, dict[str, float]], minutes: int) -> dict[str, Any]:
    eligible = {symbol: item for symbol, item in changes.items() if item.get("qvol", 0) >= MIN_QUOTE_VOLUME}
    if not eligible:
        return {"breadth": None, "gainers": [], "losers": [], "flat_volume": [], "btc": None, "eligible_count": 0}
    ups = sum(1 for item in eligible.values() if item["change_pct"] > 0.05)
    downs = sum(1 for item in eligible.values() if item["change_pct"] < -0.05)
    breadth = {"up": ups, "down": downs, "total": len(eligible)}
    btc = None
    if "BTCUSDT" in changes:
        btc = {"price": changes["BTCUSDT"]["price"], "change_pct": changes["BTCUSDT"]["change_pct"]}
    ranked = sorted(eligible.items(), key=lambda pair: pair[1]["change_pct"], reverse=True)
    floor = MIN_LIST_MOVE_PCT.get(minutes, 1.0)
    gainers = [{"symbol": symbol, **item} for symbol, item in ranked[:CANDIDATES_PER_SIDE] if item["change_pct"] >= floor][:TOP_N]
    losers = [{"symbol": symbol, **item} for symbol, item in reversed(ranked[-CANDIDATES_PER_SIDE:]) if item["change_pct"] <= -floor][:TOP_N]
    flat = sorted(((s, item) for s, item in eligible.items()
                   if abs(item["change_pct"]) < FLAT_MAX_MOVE_PCT and item.get("vol_est") is not None),
                  key=lambda pair: pair[1]["vol_est"], reverse=True)[:FLAT_CANDIDATES]
    flat_volume = [{"symbol": symbol, **item} for symbol, item in flat
                   if (item.get("vol_est") or 0) >= FLAT_MIN_RATIO and abs(item["change_pct"]) < FLAT_MAX_MOVE_PCT][:3]
    return {"breadth": breadth, "gainers": gainers, "losers": losers, "flat_volume": flat_volume,
            "btc": btc, "eligible_count": len(eligible)}
