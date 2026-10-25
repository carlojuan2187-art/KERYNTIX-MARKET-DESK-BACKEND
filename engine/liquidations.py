"""Pure aggregation and interpretation for Binance Futures forced-liquidation events.

The market feed worker owns WebSocket ingestion; this module only summarizes the
persisted event stream, preserving the original bot's side semantics: SELL closes
longs, BUY closes shorts. Binance's force-order stream is a lower bound, not a full
exchange liquidation ledger.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

DOMINANT_SHARE = 0.70
QUIET_TOTAL_USD = 250_000.0
CONCENTRATION_SHARE = 0.50
CONCENTRATION_MIN_USD = 500_000.0
PACE_NOTICE = 3.0


def summarize_events(events: Iterable[dict[str, Any]], window_minutes: int, *, now: float,
                     offset_minutes: int = 0) -> dict[str, Any]:
    end = float(now) - offset_minutes * 60
    start = end - window_minutes * 60
    long_usd = short_usd = 0.0
    long_n = short_n = 0
    by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: {"long": 0.0, "short": 0.0, "n": 0})
    biggest = None
    normalized = []
    for raw in events:
        try:
            ts = float(raw.get("timestamp", 0))
            symbol = str(raw.get("symbol") or "").upper()
            side = str(raw.get("side") or "").lower()
            usd = float(raw.get("amount_usdt", raw.get("usd", 0)))
            price = float(raw.get("price", 0) or 0)
            qty = float(raw.get("quantity", raw.get("qty", 0)) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if not symbol.endswith("USDT") or side not in {"long", "short"} or not (usd > 0 and usd < float("inf")):
            continue
        if ts < start or ts > end:
            continue
        event = {"timestamp": ts, "symbol": symbol, "side": side, "price": price, "quantity": qty, "amount_usdt": usd}
        normalized.append(event)
        row = by_symbol[symbol]
        row[side] += usd
        row["n"] += 1
        if side == "long":
            long_usd += usd
            long_n += 1
        else:
            short_usd += usd
            short_n += 1
        if biggest is None or usd > biggest["amount_usdt"]:
            biggest = event
    total = long_usd + short_usd
    top = sorted(
        ({"symbol": symbol, "usd": values["long"] + values["short"], "long": values["long"], "short": values["short"], "count": int(values["n"])}
         for symbol, values in by_symbol.items()),
        key=lambda row: row["usd"], reverse=True,
    )
    return {
        "window": window_minutes, "now": float(now), "long_usd": long_usd, "short_usd": short_usd,
        "total": total, "long_n": long_n, "short_n": short_n,
        "long_share": long_usd / total if total > 0 else None,
        "per_min": total / window_minutes, "by_symbol": dict(by_symbol), "top": top,
        "biggest": biggest, "events": normalized,
    }


def interpret_liquidations(summary: dict[str, Any], pace: float | None = None) -> list[str]:
    total = float(summary.get("total") or 0)
    if total < QUIET_TOTAL_USD:
        return ["Mercado tranquilo: casi no hay liquidaciones en esta ventana."]
    notes: list[str] = []
    share = summary.get("long_share")
    if share is not None and share >= DOMINANT_SHARE:
        notes.extend(["Barrido de longs: cierre forzado de compradores.",
                      "Si el precio se frena después del barrido, puede ser agotamiento de la caída."])
    elif share is not None and share <= 1 - DOMINANT_SHARE:
        notes.extend(["Squeeze de shorts: cierre forzado de vendedores.",
                      "Si el precio se frena después del squeeze, puede ser agotamiento de la subida."])
    else:
        notes.append("Liquidaciones repartidas entre longs y shorts: sin barrido claro de un lado.")
    top = (summary.get("top") or [None])[0]
    if top and total >= CONCENTRATION_MIN_USD and top["usd"] / total >= CONCENTRATION_SHARE:
        base = top["symbol"].removesuffix("USDT")
        notes.append(f"Concentrado en {base} ({top['usd'] / total * 100:.0f}% del total): es un movimiento propio de ese par, no de todo el mercado.")
    if pace is not None and pace >= PACE_NOTICE:
        notes.append(f"Ritmo x{pace:.1f} sobre lo normal de las últimas horas: posible cascada.")
    return notes
