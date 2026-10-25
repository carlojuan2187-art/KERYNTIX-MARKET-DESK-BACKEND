"""API adapter around the original bot's pure risk calculator."""
from __future__ import annotations

from typing import Any, Mapping

from engine.risk_calc import (
    MAINTENANCE_MARGIN_RATE,
    TAKER_FEE_RATE,
    RiskInputError,
    calculate_position,
)


def calculate_for_web(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Calculate using the bot's original rules and map to the existing web contract.

    Risk sizing, fees, leverage limits, and feasibility are copied from the original
    `app/risk_calc.py`; this function only shapes the response expected by React.
    """
    capital = float(payload["capital"])
    risk_pct = float(payload["risk_pct"])
    entry = float(payload["entry"])
    stop = float(payload["stop"])
    direction = payload.get("direction")
    targets = [float(payload["tp1"])]
    if payload.get("tp2") is not None:
        targets.append(float(payload["tp2"]))
    if payload.get("tp3") is not None:
        targets.append(float(payload["tp3"]))

    plan = calculate_position(
        capital=capital,
        risk_pct=risk_pct,
        entry=entry,
        stop=stop,
        take_profits=targets,
        direction=direction,
    )

    # Estimate liquidation at the minimum leverage required for this notional.
    # This is an approximation using the bot's 0.5% maintenance-margin constant,
    # not an exchange-specific liquidation calculation.
    sign = 1.0 if plan["direction"] == "LONG" else -1.0
    liquidation = max(
        0.0,
        entry * (1.0 - sign * (1.0 / plan["min_leverage"] - MAINTENANCE_MARGIN_RATE)),
    )
    results = []
    for target in plan["targets"]:
        results.append({
            "level": f"TP{target['index']}",
            "price": target["price"],
            "r": target["rr"],
            "usdt": target["profit"],
            # The original bot estimates round-trip fees from position notional.
            # Keep that rule rather than silently substituting another model.
            "fees_usdt": plan["fees"],
            "net_usdt": target["net_profit"],
        })

    return {
        "risk_usdt": plan["risk_usdt"],
        "qty": plan["qty"],
        "position_size": plan["notional"],
        "leverage_min": plan["min_leverage"],
        "isolated_margin": plan["margin_at_min"],
        "liquidation_approx": liquidation,
        "fee_taker_pct": TAKER_FEE_RATE * 100.0,
        "fees_usdt": plan["fees"],
        "direction": plan["direction"],
        "results": results,
        # Additional source-of-truth fields help the web explain prudent limits.
        "leverage_max_prudent": plan["max_leverage"],
        "risk_feasible": plan["feasible"],
        "net_loss_at_sl": plan["net_loss_at_sl"],
        "maintenance_margin_rate": MAINTENANCE_MARGIN_RATE,
        "model": "ftrade_bot_risk_calc_v1",
    }
