# app/risk_calc.py
"""Calculadora de riesgo y tamaño de posición para futuros USDT.

Módulo puro: no toca Mongo, Binance ni Telegram. Recibe números y devuelve un
plan de posición (`calculate_position`) o el texto listo para Telegram
(`format_risk_plan`, HTML).

Modelo (deliberadamente simple y conservador):
- Riesgo en USDT = capital × riesgo%.
- Cantidad = riesgo / distancia (entrada → stop).
- Margen aislado; la liquidación se aproxima como 1/apalancamiento − MMR.
- Comisión taker estimada en ambos lados (entrada y salida).
"""

from __future__ import annotations

import math
import re
from html import escape
from typing import Any, Dict, List, Optional, Sequence

PARSE_MODE = "HTML"

# Comisión taker típica de futuros USDT por lado (0.05%).
TAKER_FEE_RATE = 0.0005
# Tasa de mantenimiento aproximada para estimar la liquidación.
MAINTENANCE_MARGIN_RATE = 0.005
# La liquidación debe quedar al menos 1.5x más lejos que el stop.
LIQUIDATION_BUFFER = 1.5
# Tope de prudencia para el apalancamiento máximo que se muestra.
MAX_LEVERAGE_CAP = 25

MIN_RISK_PCT = 0.1
MAX_RISK_PCT = 10.0
HIGH_RISK_PCT = 3.0

_LINE = "━━━━━━━━━━━━━━━━━━━━"
_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


class RiskInputError(ValueError):
    """Entrada inválida; el mensaje está pensado para mostrarse al usuario."""


# ------------------------------------------------------------------
# Parseo
# ------------------------------------------------------------------
def parse_numbers(text: str) -> List[float]:
    """Extrae todos los números de un texto libre (acepta coma o punto decimal).

    "1000 1%" -> [1000.0, 1.0] · "$1000, riesgo 1,5" -> [1000.0, 1.5]
    """
    values: List[float] = []
    for token in _NUMBER_RE.findall(text or ""):
        values.append(float(token.replace(",", ".")))
    return values


# ------------------------------------------------------------------
# Formato numérico (sin notación científica)
# ------------------------------------------------------------------
def fmt_price(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{number:.8f}".rstrip("0").rstrip(".")


def fmt_money(value: float) -> str:
    return f"{value:.2f}"


def fmt_qty(value: float) -> str:
    if value >= 100:
        return f"{value:.0f}"
    if value >= 1:
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


# ------------------------------------------------------------------
# Cálculo
# ------------------------------------------------------------------
def validate_account(capital: float, risk_pct: float) -> None:
    if capital <= 0:
        raise RiskInputError("El capital debe ser mayor que 0.")
    if risk_pct < MIN_RISK_PCT or risk_pct > MAX_RISK_PCT:
        raise RiskInputError(
            f"El riesgo por operación debe estar entre {MIN_RISK_PCT:g}% y {MAX_RISK_PCT:g}%."
        )


def calculate_position(
    capital: float,
    risk_pct: float,
    entry: float,
    stop: float,
    take_profits: Sequence[float] = (),
    direction: Optional[str] = None,
    fee_rate: float = TAKER_FEE_RATE,
) -> Dict[str, Any]:
    """Calcula tamaño de posición, apalancamiento y resultados en SL/TP.

    `direction` ("LONG"/"SHORT") es opcional: si no se pasa se infiere de la
    posición del stop respecto a la entrada. Si se pasa y contradice al stop se
    lanza `RiskInputError`.
    """
    validate_account(capital, risk_pct)
    if entry <= 0 or stop <= 0:
        raise RiskInputError("Entrada y Stop Loss deben ser mayores que 0.")
    if math.isclose(entry, stop, rel_tol=1e-12):
        raise RiskInputError("El Stop Loss no puede ser igual a la entrada.")

    inferred = "LONG" if stop < entry else "SHORT"
    if direction:
        wanted = str(direction).upper()
        if wanted not in ("LONG", "SHORT"):
            raise RiskInputError("La dirección debe ser LONG o SHORT.")
        if wanted != inferred:
            side = "debajo" if wanted == "LONG" else "encima"
            raise RiskInputError(f"En un {wanted} el Stop Loss debe estar {side} de la entrada.")
    is_long = inferred == "LONG"

    distance = abs(entry - stop)
    stop_frac = distance / entry
    risk_usdt = capital * risk_pct / 100.0
    qty = risk_usdt / distance
    notional = qty * entry
    fees = notional * fee_rate * 2.0

    min_leverage = max(1, math.ceil(notional / capital - 1e-9))
    raw_max = math.floor(1.0 / (LIQUIDATION_BUFFER * stop_frac + MAINTENANCE_MARGIN_RATE))
    max_leverage = max(1, min(MAX_LEVERAGE_CAP, raw_max))
    feasible = min_leverage <= max_leverage

    targets: List[Dict[str, float]] = []
    for index, raw_tp in enumerate(take_profits, start=1):
        tp = float(raw_tp)
        if tp <= 0:
            raise RiskInputError(f"TP{index} debe ser mayor que 0.")
        in_profit_side = tp > entry if is_long else tp < entry
        if not in_profit_side:
            side = "encima" if is_long else "debajo"
            raise RiskInputError(f"En un {inferred} el TP{index} debe estar {side} de la entrada.")
        gain = qty * abs(tp - entry)
        targets.append({
            "index": index,
            "price": tp,
            "pct": (tp - entry) / entry * 100.0,
            "rr": abs(tp - entry) / distance,
            "profit": gain,
            "net_profit": gain - fees,
        })

    return {
        "direction": inferred,
        "capital": capital,
        "risk_pct": risk_pct,
        "risk_usdt": risk_usdt,
        "entry": entry,
        "stop": stop,
        "stop_pct": (stop - entry) / entry * 100.0,
        "qty": qty,
        "notional": notional,
        "fees": fees,
        "net_loss_at_sl": risk_usdt + fees,
        "min_leverage": min_leverage,
        "max_leverage": max_leverage,
        "margin_at_min": notional / min_leverage,
        "feasible": feasible,
        "targets": targets,
    }


# ------------------------------------------------------------------
# Mensaje de Telegram (HTML)
# ------------------------------------------------------------------
def format_risk_plan(plan: Dict[str, Any], symbol: Optional[str] = None) -> str:
    is_long = plan["direction"] == "LONG"
    badge = "🟢 LONG ▲" if is_long else "🔴 SHORT ▼"
    base = escape(str(symbol).upper().removesuffix("USDT")) if symbol else "unid."
    title = f"<b>{escape(str(symbol).upper())}</b>   {badge}" if symbol else f"<b>{badge}</b>"

    lines: List[str] = [
        "🧮 <b>CALCULADORA DE RIESGO</b>",
        _LINE,
        title,
        f"Capital {fmt_money(plan['capital'])} USDT · Riesgo {plan['risk_pct']:g}% "
        f"(<b>{fmt_money(plan['risk_usdt'])} USDT</b>)",
        "",
        f"📍 Entrada <code>{fmt_price(plan['entry'])}</code>",
        f"🛡 Stop <code>{fmt_price(plan['stop'])}</code> ({plan['stop_pct']:+.2f}%)",
        "",
        "📦 <b>TAMAÑO DE POSICIÓN</b>",
        f"Cantidad: <code>{fmt_qty(plan['qty'])}</code> {base}",
        f"Valor de posición: <code>{fmt_money(plan['notional'])}</code> USDT",
    ]

    if plan["feasible"]:
        lines.append(
            f"Apalancamiento: mín. <b>{plan['min_leverage']}x</b> · máx. prudente {plan['max_leverage']}x"
        )
        lines.append(
            f"Margen con {plan['min_leverage']}x: <code>{fmt_money(plan['margin_at_min'])}</code> USDT"
        )
    else:
        lines.append(
            f"⛔ Necesitas al menos {plan['min_leverage']}x, pero con este stop la liquidación "
            f"quedaría demasiado cerca (máx. prudente {plan['max_leverage']}x). "
            "Baja el riesgo % o no tomes esta operación."
        )

    lines += ["", f"🔻 En Stop Loss: <b>-{fmt_money(plan['risk_usdt'])}</b> USDT"]
    if plan["targets"]:
        lines.append("🎯 <b>Si llega a objetivo</b>")
        for target in plan["targets"]:
            lines.append(
                f"TP{target['index']}  <code>{fmt_price(target['price'])}</code>  "
                f"<b>+{fmt_money(target['profit'])}</b> USDT · {target['rr']:.1f}R"
            )

    lines += [
        "",
        f"Comisiones aprox. (ida y vuelta): {fmt_money(plan['fees'])} USDT",
    ]
    if plan["risk_pct"] > HIGH_RISK_PCT:
        lines.append(f"⚠️ Riesgo alto: lo habitual es 0.5%–2% por operación.")

    lines += [
        _LINE,
        "<i>Estimación con comisión taker 0.05% y margen aislado. "
        "Verifica con tu exchange. No es asesoría financiera.</i>",
    ]
    return "\n".join(lines)


def format_input_error(message: str) -> str:
    return f"❌ {message}"
