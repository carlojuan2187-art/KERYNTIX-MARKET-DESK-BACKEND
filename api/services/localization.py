"""Small server-side localization helpers for durable user notifications.

Notification text is localized before it is persisted so the in-app inbox,
Telegram and Web Push all receive the same language. Unknown text is preserved
rather than guessed.
"""
from __future__ import annotations

import re
from typing import Any


def _language(value: Any) -> str:
    return "es" if str(value or "en").strip().lower() == "es" else "en"


def localize_notification(title: str, message: str, language: Any = "en") -> tuple[str, str]:
    title = str(title or "").strip()
    message = str(message or "").strip()
    if _language(language) == "es":
        return title, message

    match = re.match(r"^Nueva señal (LONG|SHORT) · ([A-Z0-9_]+)$", title)
    if match:
        direction, symbol = match.groups()
        title = f"New {direction} signal · {symbol}"
        message = re.sub(r"^KERYNTIX MARKET DESK publicó una nueva señal (LONG|SHORT) para ([A-Z0-9_]+)\.",
                         r"KERYNTIX MARKET DESK published a new \1 signal for \2.", message)
        message = re.sub(r"(?<![A-Za-z])Entrada:", "Entry:", message)
        message = re.sub(r"(?<![A-Za-z])Stop-loss:", "Stop-loss:", message)
        message = re.sub(r"(?<![A-Za-z])Objetivos:", "Targets:", message)
        message = re.sub(r"(?<![A-Za-z])Score:", "Score:", message)
        return title, message

    match = re.match(r"^Alerta de precio · ([A-Z0-9_]+)$", title)
    if match:
        symbol = match.group(1)
        title = f"Price alert · {symbol}"
        match_msg = re.match(
            rf"^{re.escape(symbol)}: el precio actual \((.+?)\) (alcanzó o superó|alcanzó o cayó por debajo de) tu nivel de alerta \((.+?)\)\.$",
            message,
        )
        if match_msg:
            current, direction, target = match_msg.groups()
            verb = "reached or exceeded" if direction == "alcanzó o superó" else "reached or fell below"
            message = f"{symbol}: the current price ({current}) {verb} your alert level ({target})."
        return title, message

    if title == "Barrido de liquidaciones del mercado":
        title = "Market-wide liquidation sweep"
        match_msg = re.match(
            r"^Se detectaron al menos (\$[\d,]+) en liquidaciones en 5 minutos\. Predominan los (longs|shorts) \((\d+)%\)\. Mayores pares: (.+)\.$",
            message,
        )
        if match_msg:
            amount, side, share, pairs = match_msg.groups()
            message = (f"At least {amount} in liquidations were detected over 5 minutes. "
                       f"{side.capitalize()} dominate ({share}%). Largest pairs: {pairs}.")
        return title, message

    match = re.match(r"^Liquidaciones concentradas · ([A-Z0-9_]+)$", title)
    if match:
        symbol = match.group(1)
        title = f"Concentrated liquidations · {symbol}"
        match_msg = re.match(
            rf"^Se detectaron al menos (\$[\d,]+) liquidados en {re.escape(symbol)} en 5 minutos; predominan los (longs|shorts)\.$",
            message,
        )
        if match_msg:
            amount, side = match_msg.groups()
            message = f"At least {amount} in liquidations were detected on {symbol} over 5 minutes; {side} dominate."
        return title, message

    match = re.match(r"^Aviso (PUMP|DUMP) · ([A-Z0-9_]+)$", title)
    if match:
        side, symbol = match.groups()
        title = f"{side} alert · {symbol}"
        match_msg = re.match(
            rf"^{re.escape(symbol)} se movió ([+-]?[\d.]+)% en (\d+) min con volumen x([\d.]+) de lo normal\. Este aviso describe una anomalía de mercado, no una señal de entrada\.$",
            message,
        )
        if match_msg:
            change, window, ratio = match_msg.groups()
            message = (f"{symbol} moved {change}% in {window} min with {ratio}x normal volume. "
                       "This alert describes a market anomaly, not an entry signal.")
        return title, message

    match = re.match(r"^Volumen anormal · ([A-Z0-9_]+)$", title)
    if match:
        symbol = match.group(1)
        title = f"Abnormal volume · {symbol}"
        match_msg = re.match(
            rf"^{re.escape(symbol)} muestra volumen x([\d.]+) de lo normal con un movimiento de solo ([+-]?[\d.]+)% en 15 minutos\.$",
            message,
        )
        if match_msg:
            ratio, change = match_msg.groups()
            message = f"{symbol} is showing {ratio}x normal volume with a move of only {change}% over 15 minutes."
        return title, message

    exact = {
        "Tu periodo de prueba termina pronto": "Your trial period ends soon",
        "Tu periodo de prueba ha terminado": "Your trial period has ended",
        "Tu suscripción Premium termina pronto": "Your Premium subscription ends soon",
        "Tu suscripción Premium ha terminado": "Your Premium subscription has ended",
        "Actualización de tu plan KERYNTIX MARKET DESK": "KERYNTIX MARKET DESK plan update",
        "Tu periodo de prueba de KERYNTIX MARKET DESK termina pronto. Revisa tus opciones de suscripción para mantener el acceso.":
            "Your KERYNTIX MARKET DESK trial ends soon. Review your subscription options to keep access.",
        "Tu periodo de prueba de KERYNTIX MARKET DESK ha terminado. Puedes revisar los planes disponibles desde tu cuenta.":
            "Your KERYNTIX MARKET DESK trial has ended. You can review available plans from your account.",
        "Tu suscripción Premium de KERYNTIX MARKET DESK termina pronto. Revisa tu cuenta para renovarla.":
            "Your KERYNTIX MARKET DESK Premium subscription ends soon. Review your account to renew it.",
        "Tu periodo Premium de KERYNTIX MARKET DESK ha terminado. Puedes revisar las opciones de renovación desde tu cuenta.":
            "Your KERYNTIX MARKET DESK Premium period has ended. You can review renewal options from your account.",
        "Una persona invitada mediante tu enlace ha activado Premium.":
            "Someone invited through your referral link has activated Premium.",
        "Un referido activó Premium": "A referral activated Premium",
        "Has recibido días Premium": "You received Premium days",
    }
    title = exact.get(title, title)
    message = exact.get(message, message)
    match = re.match(r"^Has recibido (\d+) días adicionales de Premium por tus referidos\.$", message)
    if match:
        message = f"You received {match.group(1)} additional Premium days from your referrals."
    match = re.match(r"^Tu periodo Premium de KERYNTIX MARKET DESK ha terminado, pero tu periodo de prueba sigue activo hasta (.+)\. Puedes revisar los planes disponibles desde tu cuenta\.$", message)
    if match:
        message = (f"Your KERYNTIX MARKET DESK Premium period has ended, but your trial remains active until {match.group(1)}. "
                   "You can review available plans from your account.")
    return title, message
