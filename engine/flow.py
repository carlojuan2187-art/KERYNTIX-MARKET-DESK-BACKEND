# app/flow.py
"""🌊 Flujo comprador/vendedor (delta / CVD) de un par de Binance Futures.

Cada vela de Binance trae `taker_buy_base`/`taker_buy_quote`: el volumen comprado
"a mercado" (compra agresiva). Lo demás es venta agresiva. Con eso se calcula:

- % de compra agresiva y delta (compra - venta) en 15m / 1h / 4h,
- tendencia del delta acumulado (CVD) como mini gráfico,
- volumen actual frente a su normal reciente,
- una lectura que cruza precio vs delta (divergencias).

Una sola llamada REST por par (velas de 5m) alimenta todo. El módulo es puro salvo
`fetch_candles`: `get_json` es inyectable para pruebas. Las velas se cachean unos
segundos y las comparte `app/movers.py`. Informativo: no modifica ni filtra señales.
"""

from __future__ import annotations

import logging
import os
import re
import statistics
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from engine.derivatives import BINANCE_FUTURES_API, REQUEST_TIMEOUT, fmt_big, validate_symbol

logger = logging.getLogger(__name__)

CANDLE_MINUTES = 5
CANDLE_LIMIT = 240                     # 20 h: 4 h actuales + 3 ventanas históricas para comparar volumen
CACHE_SECONDS = max(5, int(os.getenv("FLOW_CACHE_SECONDS", "20")))

# (minutos, etiqueta). El número de velas sale de minutos / 5.
WINDOWS = ((15, "15m"), (60, "1h"), (240, "4h"))

BUY_SIDE = 52.0          # % de compra a partir del cual manda el comprador
SELL_SIDE = 48.0         # % de compra por debajo del cual manda el vendedor
STRONG_SIDE = 65.0       # desequilibrio fuerte (65/35)
PRICE_MOVE_NOTICE = 0.30  # % de movimiento de precio que cuenta como "se movió"
VOLUME_NOTICE = 2.0      # x del volumen normal que se menciona

_LINE = "━━━━━━━━━━━━━━━━━━━━"
_SPARK = "▁▂▃▄▅▆▇█"


class FlowUnavailable(RuntimeError):
    """No se pudieron obtener velas del par."""


# ------------------------------------------------------------------
# Velas
# ------------------------------------------------------------------
def _default_get_json(path: str, params: Dict[str, Any]) -> Any:
    import requests  # import tardío: el módulo puro no necesita red

    response = requests.get(f"{BINANCE_FUTURES_API}{path}", params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _f(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def parse_candles(rows: Any) -> List[Dict[str, float]]:
    """Filas de /fapi/v1/klines -> lista de dicts. Descarta filas rotas."""
    candles: List[Dict[str, float]] = []
    for row in rows or []:
        try:
            values = {
                "t": float(row[0]) / 1000.0, "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]), "v": float(row[5]),
                "close_t": float(row[6]) / 1000.0, "qv": float(row[7]),
                "tb": float(row[9]), "tbq": float(row[10]),
            }
        except (TypeError, ValueError, IndexError):
            continue
        if any(v != v or abs(v) == float("inf") for v in values.values()):
            continue
        candles.append(values)
    return candles


def fetch_candles(symbol: str, get_json: Optional[Callable[[str, Dict[str, Any]], Any]] = None) -> List[Dict[str, float]]:
    symbol = validate_symbol(symbol)
    rows = (get_json or _default_get_json)(
        "/fapi/v1/klines", {"symbol": symbol, "interval": f"{CANDLE_MINUTES}m", "limit": CANDLE_LIMIT}
    )
    candles = parse_candles(rows)
    if len(candles) < 6:
        raise FlowUnavailable(f"Sin velas suficientes para {symbol}")
    return candles


_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()


def get_candles(symbol: str, max_age: float = CACHE_SECONDS, now: Optional[float] = None,
                get_json: Optional[Callable[[str, Dict[str, Any]], Any]] = None) -> List[Dict[str, float]]:
    """Velas 5m con caché corta (compartida entre usuarios y con `movers`)."""
    symbol = validate_symbol(symbol)
    current = time.time() if now is None else now
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry and current - entry["at"] < max_age:
            return entry["candles"]
    candles = fetch_candles(symbol, get_json)  # los errores no se cachean
    with _cache_lock:
        _cache[symbol] = {"at": current, "candles": candles}
        if len(_cache) > 400:  # tope defensivo
            for key in sorted(_cache, key=lambda k: _cache[k]["at"])[:100]:
                _cache.pop(key, None)
    return candles


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ------------------------------------------------------------------
# Cálculo
# ------------------------------------------------------------------
def window_stats(candles: Sequence[Dict[str, float]], minutes: int, now: Optional[float] = None) -> Optional[Dict[str, float]]:
    """Estadísticas de la ventana más reciente de `minutes`.

    Incluye la vela en curso si ya lleva >= 60 s (así el dato no se queda 5 minutos
    atrás); si acaba de abrir, usa sólo velas cerradas. El volumen se compara por
    *ritmo* (por minuto) contra la mediana de ventanas anteriores del mismo tamaño,
    de modo que la vela parcial no lo distorsiona.
    """
    n = max(1, minutes // CANDLE_MINUTES)
    if len(candles) < n + 1:
        return None
    current = time.time() if now is None else now
    last = candles[-1]
    is_open = last["close_t"] > current
    elapsed = current - last["t"]
    if is_open and elapsed < 60:
        window, covered = list(candles[-(n + 1):-1]), n * CANDLE_MINUTES
        prior = list(candles[:-(n + 1)])
    else:
        window = list(candles[-n:])
        covered = (n - 1) * CANDLE_MINUTES + (min(elapsed, CANDLE_MINUTES * 60) / 60.0 if is_open else CANDLE_MINUTES)
        prior = list(candles[:-n])
    if len(window) < n or covered <= 0:
        return None

    vol = sum(c["v"] for c in window)
    qv = sum(c["qv"] for c in window)
    tb = sum(c["tb"] for c in window)
    tbq = sum(c["tbq"] for c in window)
    if vol <= 0 or qv <= 0:
        return None
    first_open = window[0]["o"]
    price_end = window[-1]["c"]

    # Normal reciente: sumas de n velas, de atrás hacia delante, sólo velas cerradas.
    closed_prior = [c for c in prior if c["close_t"] <= current]
    groups = [
        sum(c["qv"] for c in closed_prior[i - n:i])
        for i in range(len(closed_prior), n - 1, -n)
    ]
    volume_ratio = None
    if len(groups) >= 3:
        baseline_rate = statistics.median(groups) / (n * CANDLE_MINUTES)
        if baseline_rate > 0:
            volume_ratio = (qv / covered) / baseline_rate

    sell_base, sell_quote = vol - tb, qv - tbq
    return {
        "minutes": minutes,
        "buy_pct": tb / vol * 100.0,
        "delta_base": tb - sell_base,
        "delta_quote": tbq - sell_quote,
        "volume_base": vol,
        "volume_quote": qv,
        "price_chg": (price_end / first_open - 1.0) * 100.0 if first_open > 0 else 0.0,
        "price": price_end,
        "volume_ratio": volume_ratio,
    }


def cvd_series(candles: Sequence[Dict[str, float]], points: int = 48, now: Optional[float] = None) -> List[float]:
    """Delta acumulado (USDT) de las últimas 4h, normalmente una muestra por vela de 5m."""
    n4h = 240 // CANDLE_MINUTES
    closed = list(candles) if now is None else [c for c in candles if c.get("close_t", float("inf")) <= now]
    tail = list(closed[-n4h:])
    if len(tail) < points:
        return []
    total, cumulative = 0.0, []
    for c in tail:
        total += 2.0 * c["tbq"] - c["qv"]
        cumulative.append(total)
    step = len(cumulative) / points
    return [cumulative[min(len(cumulative) - 1, int(round((i + 1) * step)) - 1)] for i in range(points)]


def compute_flow(symbol: str, candles: Sequence[Dict[str, float]], now: Optional[float] = None) -> Dict[str, Any]:
    windows: List[Dict[str, Any]] = []
    for minutes, label in WINDOWS:
        stats = window_stats(candles, minutes, now)
        if stats is not None:
            windows.append(dict(stats, label=label))
    if not windows:
        raise FlowUnavailable(f"Sin datos de flujo para {symbol}")
    return {"symbol": symbol, "windows": windows, "cvd": cvd_series(candles, now=now)}


def price_cvd_context(candles: Sequence[Dict[str, float]], now: Optional[float] = None, lookback: int = 12) -> Dict[str, Any]:
    """Compara el cambio del precio con el delta taker neto de las últimas velas cerradas.

    La divergencia es una observación descriptiva, no una señal de entrada. Se normaliza
    el delta por el volumen cotizado para que el umbral sea comparable entre símbolos.
    """
    current = time.time() if now is None else now
    closed = [c for c in candles if c.get("close_t", float("inf")) <= current]
    if len(closed) < max(lookback, 3):
        return {"status": "collecting", "label": "Recopilando", "price_change_pct": None,
                "delta_usdt": None, "delta_share_pct": None, "lookback_candles": len(closed),
                "message": "Se necesitan más velas cerradas para comparar precio y flujo."}
    tail = list(closed[-lookback:])
    first_open = tail[0].get("o", 0.0)
    last_close = tail[-1].get("c", 0.0)
    price_change = (last_close / first_open - 1.0) * 100.0 if first_open > 0 else None
    delta_usdt = sum(2.0 * c.get("tbq", 0.0) - c.get("qv", 0.0) for c in tail)
    quote_volume = sum(c.get("qv", 0.0) for c in tail)
    delta_share = delta_usdt / quote_volume * 100.0 if quote_volume > 0 else None
    if price_change is None or delta_share is None:
        status, label, message = "collecting", "Recopilando", "No hay volumen cotizado suficiente para confirmar la lectura."
    elif price_change >= PRICE_MOVE_NOTICE and delta_share <= -3.0:
        status, label, message = "bearish_divergence", "Divergencia bajista", "El precio sube mientras el delta neto de compradores agresivos es negativo."
    elif price_change <= -PRICE_MOVE_NOTICE and delta_share >= 3.0:
        status, label, message = "bullish_divergence", "Divergencia alcista", "El precio cae mientras el delta neto de compradores agresivos es positivo."
    elif price_change > 0 and delta_share > 0:
        status, label, message = "buyers_confirmed", "Compra acompaña", "El movimiento alcista coincide con delta comprador."
    elif price_change < 0 and delta_share < 0:
        status, label, message = "sellers_confirmed", "Venta acompaña", "El movimiento bajista coincide con delta vendedor."
    else:
        status, label, message = "mixed", "Sin confirmación", "El precio y el delta no muestran una confirmación clara en esta ventana."
    return {"status": status, "label": label, "price_change_pct": price_change, "delta_usdt": delta_usdt,
            "delta_share_pct": delta_share, "lookback_candles": len(tail), "message": message}


def get_flow(symbol: str, now: Optional[float] = None) -> Dict[str, Any]:
    symbol = validate_symbol(symbol)
    candles = get_candles(symbol, now=now)
    return compute_flow(symbol, candles, now)


# ------------------------------------------------------------------
# Lectura
# ------------------------------------------------------------------
def _by_label(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {w["label"]: w for w in data["windows"]}


def _side(buy_pct: float) -> int:
    return 1 if buy_pct >= BUY_SIDE else (-1 if buy_pct <= SELL_SIDE else 0)


def interpret_flow(data: Dict[str, Any]) -> List[str]:
    """Lectura breve cruzando precio y delta. Las líneas con ⚠/ℹ/✅ van sin viñeta."""
    w = _by_label(data)
    notes: List[str] = []
    w15, w1h, w4h = w.get("15m"), w.get("1h"), w.get("4h")
    ref = w1h or w15 or data["windows"][0]
    side, move = _side(ref["buy_pct"]), ref["price_chg"]

    if move >= PRICE_MOVE_NOTICE and side < 0:
        notes.append("⚠ El precio sube pero los vendedores agresivos dominan: la subida no tiene respaldo de compra.")
    elif move <= -PRICE_MOVE_NOTICE and side > 0:
        notes.append("ℹ El precio cae pero los compradores agresivos absorben la venta: si el precio se frena, puede ser un suelo.")
    elif move >= PRICE_MOVE_NOTICE and side > 0:
        notes.append("✅ Subida con respaldo: los compradores agresivos dominan.")
    elif move <= -PRICE_MOVE_NOTICE and side < 0:
        notes.append("⚠ Caída con presión vendedora real: los vendedores agresivos dominan.")
    elif side == 0:
        notes.append("Flujo equilibrado: ni compradores ni vendedores toman el control.")
    elif side > 0:
        notes.append("Compradores agresivos algo por encima, pero el precio casi no se mueve.")
    else:
        notes.append("Vendedores agresivos algo por encima, pero el precio casi no se mueve.")

    if w15 and w1h:
        s15, s1h = _side(w15["buy_pct"]), _side(w1h["buy_pct"])
        if s15 > 0 and s1h < 0:
            notes.append("En 15m entra compra; todavía no cambia el 1h.")
        elif s15 < 0 and s1h > 0:
            notes.append("En 15m aparece venta; el 1h todavía aguanta.")
    sides = [_side(x["buy_pct"]) for x in data["windows"]]
    if len(sides) == 3 and len(set(sides)) == 1 and sides[0] != 0:
        notes.append("Compra agresiva sostenida en los tres plazos." if sides[0] > 0
                     else "Venta agresiva sostenida en los tres plazos.")
    if w15 and (w15["buy_pct"] >= STRONG_SIDE or w15["buy_pct"] <= 100 - STRONG_SIDE):
        notes.append(f"Desequilibrio fuerte en 15m: {w15['buy_pct']:.0f}% compra / {100 - w15['buy_pct']:.0f}% venta.")
    if w15 and (w15.get("volume_ratio") or 0) >= VOLUME_NOTICE:
        notes.append(f"Volumen de 15m x{w15['volume_ratio']:.1f} sobre su normal: el flujo pesa más de lo habitual.")
    return notes


# ------------------------------------------------------------------
# Formato
# ------------------------------------------------------------------
def normalize_symbol(text: Any) -> str:
    """'sol', 'SOL/USDT', '$solusdt', '1000pepe' -> 'SOLUSDT', '1000PEPEUSDT'. ValueError si no es válido."""
    raw = str(text or "").strip().upper()
    if re.search(r"\s", raw):  # una frase no es un símbolo
        raise ValueError("Símbolo inválido")
    raw = re.sub(r"[/_\-:$#]", "", raw)
    base = raw[:-4] if raw.endswith("USDT") else raw
    if not base:
        raise ValueError("Símbolo vacío")
    return validate_symbol(base + "USDT")


def base_name(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def fmt_signed_big(value: Optional[float]) -> str:
    if value is None:
        return "n/d"
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return f"{sign}{fmt_big(abs(value))}"


def fmt_money(value: float) -> str:
    """Cifra corta en USD sin signo: 4.2M, 850K, 1.25B."""
    a = abs(value)
    if a >= 1e9:
        return f"{a / 1e9:.2f}B"
    if a >= 1e7:
        return f"{a / 1e6:.1f}M"
    if a >= 1e6:
        return f"{a / 1e6:.2f}M"
    if a >= 1e3:
        return f"{a / 1e3:.0f}K"
    return f"{a:.0f}"


def fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "n/d"
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return f"{sign}${fmt_money(value)}"


def fmt_pct(value: Optional[float], digits: int = 1) -> str:
    return "n/d" if value is None else f"{value:+.{digits}f}%".replace("-", "−")


def side_emoji(buy_pct: float) -> str:
    return "🟢" if buy_pct >= BUY_SIDE else ("🔴" if buy_pct <= SELL_SIDE else "⚪")


def bar(buy_pct: float, width: int = 10) -> str:
    """Barra compra/venta: ▰ compra, ▱ venta."""
    filled = max(0, min(width, int(round(buy_pct / 100.0 * width))))
    return "▰" * filled + "▱" * (width - filled)


def sparkline(values: Sequence[float]) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    if high - low <= 0:
        return _SPARK[3] * len(values)
    return "".join(_SPARK[min(7, int((v - low) / (high - low) * 7.999))] for v in values)


def local_time(now: Optional[float] = None) -> str:
    try:
        zone = ZoneInfo(os.getenv("USER_TIMEZONE", "America/Havana"))
    except Exception:  # noqa: BLE001
        zone = ZoneInfo("UTC")
    moment = datetime.fromtimestamp(time.time() if now is None else now, tz=zone)
    return moment.strftime("%H:%M:%S")


def format_flow_message(data: Dict[str, Any], now: Optional[float] = None) -> str:
    symbol = data["symbol"]
    coin = base_name(symbol)
    w = _by_label(data)
    lines: List[str] = [f"🌊 FLUJO · {symbol}", _LINE, "Compra agresiva vs venta agresiva"]

    for _minutes, label in WINDOWS:
        item = w.get(label)
        if not item:
            continue
        lines.append(f"{label:<3} {side_emoji(item['buy_pct'])} {item['buy_pct']:.0f}% compra  {bar(item['buy_pct'])}")
        lines.append(f"      Δ {fmt_signed_big(item['delta_base'])} {coin} · {fmt_usd(item['delta_quote'])}")

    price_parts = [f"{label} {fmt_pct(w[label]['price_chg'])}" for _m, label in WINDOWS if label in w]
    lines += ["", "Precio  " + " · ".join(price_parts)]
    if data.get("cvd"):
        cvd = data["cvd"]
        trend = "al alza ↗" if cvd[-1] > cvd[0] else ("a la baja ↘" if cvd[-1] < cvd[0] else "plano →")
        lines.append(f"CVD 4h  {sparkline(cvd)}  {trend}")
    w15 = w.get("15m")
    if w15 and w15.get("volume_ratio") is not None:
        lines.append(f"Volumen 15m  x{w15['volume_ratio']:.1f} de lo normal")

    notes = interpret_flow(data)
    if notes:
        lines += ["", "🔎 Lectura"]
        lines += [n if n[:1] in "⚠ℹ✅" else f"• {n}" for n in notes]

    lines += [
        _LINE,
        f"Datos de Binance Futures · {local_time(now)}",
        "Delta = compra agresiva − venta agresiva. Informativo: no modifica ni filtra señales.",
    ]
    return "\n".join(lines)
