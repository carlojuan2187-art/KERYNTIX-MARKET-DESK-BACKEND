# app/derivatives.py
"""💰 Derivados: funding, open interest y posicionamiento long/short de un par.

- `fetch_derivatives` descarga (con `get_json` inyectable para tests) y devuelve un
  diccionario con métricas opcionales: si una falla, el resto se muestra igual.
- `interpret` y `format_derivatives_message` son puros (sin red ni Telegram).
- `get_derivatives` añade una caché corta en memoria: muchos usuarios pulsando el
  mismo par no multiplican las llamadas a Binance.

Es informativo: no filtra ni modifica señales.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

BINANCE_FUTURES_API = os.getenv("BINANCE_FUTURES_API", "https://fapi.binance.com")
REQUEST_TIMEOUT = int(os.getenv("DERIVATIVES_TIMEOUT", "8"))
CACHE_SECONDS = max(15, int(os.getenv("DERIVATIVES_CACHE_SECONDS", "60")))

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")

# Umbrales de lectura.
FUNDING_NOTICE = 0.0003      # 0.03 %
FUNDING_CROWDED = 0.0005     # 0.05 %
LS_LEANING = 1.5             # ratio de cuentas: inclinado
LS_CROWDED = 2.0             # ratio de cuentas: muy cargado
OI_MOVE_PCT = 5.0            # cambio de OI en 24h considerado relevante

_LINE = "━━━━━━━━━━━━━━━━━━━━"


class DerivativesUnavailable(RuntimeError):
    """No se pudo obtener ninguna métrica del par."""


# ------------------------------------------------------------------
# Descarga
# ------------------------------------------------------------------
def _default_get_json(path: str, params: Dict[str, Any]) -> Any:
    import requests  # import tardío: el módulo puro no necesita red

    response = requests.get(f"{BINANCE_FUTURES_API}{path}", params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def _to_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old <= 0:
        return None
    return (new / old - 1.0) * 100.0


def validate_symbol(symbol: str) -> str:
    symbol = str(symbol or "").upper()
    if not SYMBOL_RE.match(symbol):
        raise ValueError(f"Símbolo inválido: {symbol!r}")
    return symbol


def fetch_derivatives(symbol: str, get_json: Optional[Callable[[str, Dict[str, Any]], Any]] = None) -> Dict[str, Any]:
    """Métricas de derivados de un par. Cada bloque es independiente y opcional."""
    symbol = validate_symbol(symbol)
    get = get_json or _default_get_json
    data: Dict[str, Any] = {
        "symbol": symbol, "funding": None, "next_funding_ms": None, "premium_pct": None,
        "oi_value": None, "oi_change_1h": None, "oi_change_24h": None,
        "ls_ratio": None, "ls_long_pct": None, "ls_ratio_24h": None, "top_ratio": None,
    }

    try:
        premium = get("/fapi/v1/premiumIndex", {"symbol": symbol})
        data["funding"] = _to_float(premium.get("lastFundingRate"))
        data["next_funding_ms"] = _to_float(premium.get("nextFundingTime"))
        mark, index = _to_float(premium.get("markPrice")), _to_float(premium.get("indexPrice"))
        if mark and index:
            data["premium_pct"] = (mark / index - 1.0) * 100.0
    except Exception as exc:
        logger.debug("Funding no disponible para %s: %s", symbol, exc)

    try:
        rows = get("/futures/data/openInterestHist", {"symbol": symbol, "period": "1h", "limit": 25})
        values = [_to_float(r.get("sumOpenInterestValue")) for r in rows]
        values = [v for v in values if v is not None]
        if values:
            data["oi_value"] = values[-1]
            if len(values) >= 2:
                data["oi_change_1h"] = _pct_change(values[-1], values[-2])
                data["oi_change_24h"] = _pct_change(values[-1], values[0])
    except Exception as exc:
        logger.debug("Open interest no disponible para %s: %s", symbol, exc)

    try:
        rows = get("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": "1h", "limit": 25})
        if rows:
            last = rows[-1]
            data["ls_ratio"] = _to_float(last.get("longShortRatio"))
            long_acc = _to_float(last.get("longAccount"))
            data["ls_long_pct"] = long_acc * 100.0 if long_acc is not None else None
            data["ls_ratio_24h"] = _to_float(rows[0].get("longShortRatio")) if len(rows) > 1 else None
    except Exception as exc:
        logger.debug("Long/short no disponible para %s: %s", symbol, exc)

    try:
        rows = get("/futures/data/topLongShortPositionRatio", {"symbol": symbol, "period": "1h", "limit": 1})
        if rows:
            data["top_ratio"] = _to_float(rows[-1].get("longShortRatio"))
    except Exception as exc:
        logger.debug("Top traders no disponible para %s: %s", symbol, exc)

    if all(data[k] is None for k in ("funding", "oi_value", "ls_ratio", "top_ratio")):
        raise DerivativesUnavailable(f"Sin datos de derivados para {symbol}")
    return data


# ------------------------------------------------------------------
# Caché
# ------------------------------------------------------------------
_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()


def get_derivatives(symbol: str, now: Optional[float] = None) -> Dict[str, Any]:
    """Como `fetch_derivatives` pero con caché de `CACHE_SECONDS` por par."""
    symbol = validate_symbol(symbol)
    current = time.time() if now is None else now
    with _cache_lock:
        entry = _cache.get(symbol)
        if entry and current - entry["at"] < CACHE_SECONDS:
            return dict(entry["data"], fetched_at=entry["at"])
    data = fetch_derivatives(symbol)  # los errores no se cachean
    with _cache_lock:
        _cache[symbol] = {"at": current, "data": data}
        if len(_cache) > 200:  # tope defensivo
            for key in sorted(_cache, key=lambda k: _cache[k]["at"])[:50]:
                _cache.pop(key, None)
    return dict(data, fetched_at=current)


_funding_cache: Dict[str, Dict[str, Any]] = {}


def get_next_funding_ms(symbol: str = "BTCUSDT", now: Optional[float] = None,
                        get_json: Optional[Callable[[str, Dict[str, Any]], Any]] = None) -> Optional[float]:
    """Hora (ms epoch) del próximo funding de un par, con una sola llamada y caché corta.

    Devuelve None si Binance no responde (el llamador usa su propio respaldo).
    """
    symbol = validate_symbol(symbol)
    current = time.time() if now is None else now
    with _cache_lock:
        entry = _funding_cache.get(symbol)
        if entry and current - entry["at"] < CACHE_SECONDS:
            return entry["ms"]
    try:
        premium = (get_json or _default_get_json)("/fapi/v1/premiumIndex", {"symbol": symbol})
        ms = _to_float(premium.get("nextFundingTime"))
    except Exception as exc:
        logger.debug("Próximo funding no disponible para %s: %s", symbol, exc)
        return None
    if ms is None:
        return None
    with _cache_lock:
        _funding_cache[symbol] = {"at": current, "ms": ms}
    return ms


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _funding_cache.clear()


# ------------------------------------------------------------------
# Interpretación
# ------------------------------------------------------------------
def interpret(data: Dict[str, Any], direction: Optional[str] = None) -> List[str]:
    """Lectura breve del posicionamiento; con `direction` añade el contexto de la señal."""
    notes: List[str] = []
    funding = data.get("funding")
    ls = data.get("ls_ratio")
    oi24 = data.get("oi_change_24h")

    longs_crowded = (funding is not None and funding >= FUNDING_NOTICE) or (ls is not None and ls >= LS_LEANING)
    shorts_crowded = (funding is not None and funding <= -FUNDING_NOTICE) or (ls is not None and ls <= 1.0 / LS_LEANING)

    if funding is not None:
        if funding >= FUNDING_CROWDED:
            notes.append("Funding alto: los longs pagan mucho; mercado muy cargado al alza.")
        elif funding >= FUNDING_NOTICE:
            notes.append("Funding algo elevado: ligero sesgo comprador.")
        elif funding <= -FUNDING_CROWDED:
            notes.append("Funding muy negativo: los shorts pagan mucho; mercado muy cargado a la baja.")
        elif funding <= -FUNDING_NOTICE:
            notes.append("Funding negativo: ligero sesgo vendedor.")
        else:
            notes.append("Funding neutral.")

    if ls is not None:
        if ls >= LS_CROWDED:
            notes.append("Cuentas muy cargadas a long.")
        elif ls >= LS_LEANING:
            notes.append("Cuentas inclinadas a long.")
        elif ls <= 1.0 / LS_CROWDED:
            notes.append("Cuentas muy cargadas a short.")
        elif ls <= 1.0 / LS_LEANING:
            notes.append("Cuentas inclinadas a short.")
        else:
            notes.append("Cuentas equilibradas entre long y short.")

    if oi24 is not None:
        if oi24 >= OI_MOVE_PCT:
            notes.append("OI en alza: entra posicionamiento nuevo (más apalancamiento en juego).")
        elif oi24 <= -OI_MOVE_PCT:
            notes.append("OI a la baja: se están cerrando posiciones.")

    side = str(direction or "").upper()
    if side == "LONG":
        if longs_crowded and not shorts_crowded:
            notes.append("⚠️ Tu LONG va con la mayoría: hay más combustible para un barrido contra longs.")
        elif shorts_crowded and not longs_crowded:
            notes.append("ℹ️ Tu LONG va contra la mayoría: los shorts están cargados.")
        else:
            notes.append("Sin sesgo claro de posicionamiento para tu LONG.")
    elif side == "SHORT":
        if shorts_crowded and not longs_crowded:
            notes.append("⚠️ Tu SHORT va con la mayoría: hay más combustible para un barrido contra shorts.")
        elif longs_crowded and not shorts_crowded:
            notes.append("ℹ️ Tu SHORT va contra la mayoría: los longs están cargados.")
        else:
            notes.append("Sin sesgo claro de posicionamiento para tu SHORT.")
    return notes


# ------------------------------------------------------------------
# Formato
# ------------------------------------------------------------------
def fmt_big(value: Optional[float]) -> str:
    if value is None:
        return "n/d"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return f"{value / limit:.2f}{suffix}"
    return f"{value:.0f}"


def _fmt_signed_pct(value: Optional[float], digits: int = 1) -> str:
    return "n/d" if value is None else f"{value:+.{digits}f}%"


def fmt_countdown(next_ms: Optional[float], now: Optional[float] = None) -> str:
    if next_ms is None:
        return "n/d"
    seconds = next_ms / 1000.0 - (time.time() if now is None else now)
    if seconds <= 60:
        return "en breve"
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return f"en {hours}h {minutes}m" if hours else f"en {minutes}m"


def format_derivatives_message(
    data: Dict[str, Any],
    direction: Optional[str] = None,
    now: Optional[float] = None,
) -> str:
    symbol = data["symbol"]
    header = f"💰 DERIVADOS · {symbol}"
    if direction:
        header += f" · {str(direction).upper()}"
    lines: List[str] = [header, _LINE]

    funding = data.get("funding")
    if funding is not None:
        payer = "longs pagan a shorts" if funding > 0 else ("shorts pagan a longs" if funding < 0 else "sin pagos")
        lines.append(f"Funding: {funding * 100:+.4f}% · {payer}")
        lines.append(f"Próximo cobro: {fmt_countdown(data.get('next_funding_ms'), now)}")
        if data.get("premium_pct") is not None:
            lines.append(f"Prima mark/index: {data['premium_pct']:+.3f}%")
    else:
        lines.append("Funding: n/d")

    lines.append("")
    if data.get("oi_value") is not None:
        lines.append(f"Open interest: {fmt_big(data['oi_value'])} USDT")
        lines.append(
            f"  1h {_fmt_signed_pct(data.get('oi_change_1h'))} · 24h {_fmt_signed_pct(data.get('oi_change_24h'))}"
        )
    else:
        lines.append("Open interest: n/d")

    if data.get("ls_ratio") is not None:
        long_pct = data.get("ls_long_pct")
        split = f" ({long_pct:.0f}% long / {100 - long_pct:.0f}% short)" if long_pct is not None else ""
        lines.append(f"Cuentas long/short: {data['ls_ratio']:.2f}{split}")
        if data.get("ls_ratio_24h") is not None:
            lines.append(f"  hace 24h: {data['ls_ratio_24h']:.2f}")
    else:
        lines.append("Cuentas long/short: n/d")
    if data.get("top_ratio") is not None:
        lines.append(f"Top traders (posiciones): {data['top_ratio']:.2f}")

    notes = interpret(data, direction)
    if notes:
        lines += ["", "🔎 Lectura"]
        lines += [n if n[:1] in "⚠ℹ" else f"• {n}" for n in notes]

    lines += [_LINE, "Datos de Binance Futures. Informativo: no modifica ni filtra señales."]
    return "\n".join(lines)
