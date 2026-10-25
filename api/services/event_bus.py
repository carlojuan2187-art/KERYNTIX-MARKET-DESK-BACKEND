"""Best-effort publishers for the public real-time API contract.

Business mutations are persisted before this module is called. A transient Redis
failure must not roll back a paid subscription or a moderation action; clients can
re-fetch canonical state over REST after reconnecting.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi.encoders import jsonable_encoder

logger = logging.getLogger("ftrade.event_bus")
ACCOUNT_EVENTS = {"plan.changed", "ban.changed", "payment.updated"}


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def contract_payment_record(row: Mapping[str, Any]) -> dict[str, Any]:
    internal = str(row.get("status", "pending")).lower()
    contract_status = "confirmed" if internal in {"verified", "completed", "confirmed"} else internal
    return jsonable_encoder({
        "id": str(row.get("payment_id") or row.get("id") or ""),
        "record_id": str(row.get("payment_id") or row.get("id") or ""),
        "amount_usdt": str(row.get("amount_usdt") or ""),
        "wallet_from": row.get("sender_address") or row.get("wallet_from"),
        "status": contract_status,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at") or row.get("completed_at"),
        "confirmations": row.get("confirmations"),
        "tx_hash": row.get("tx_hash"),
        "expires_at": row.get("expires_at"),
    })


async def publish_account_event(cache: Any, user_id: str, event: str, payload: Mapping[str, Any]) -> None:
    if event not in ACCOUNT_EVENTS:
        raise ValueError("UNSUPPORTED_ACCOUNT_EVENT")
    try:
        await cache.publish_json("account:events:v1", {
            "user_id": str(user_id), "channel": "account", "event": event,
            "payload": jsonable_encoder(dict(payload)), "ts": utc_iso(),
        })
    except Exception as exc:  # state is already durable; WebSocket is a hint, REST is canonical
        logger.warning("No se pudo publicar evento de cuenta %s (%s)", event, type(exc).__name__)


async def publish_payment_updated(cache: Any, user_id: str, payment: Mapping[str, Any]) -> None:
    payload = {"record": contract_payment_record(payment)}
    try:
        await cache.publish_json("billing:events:v1", {
            "user_id": str(user_id), "channel": "account", "event": "payment.updated",
            "payload": jsonable_encoder(payload), "ts": utc_iso(),
        })
    except Exception as exc:
        logger.warning("No se pudo publicar actualización de pago (%s)", type(exc).__name__)
