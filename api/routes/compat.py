"""Compatibility aliases matching the routes in web/API_CONTRACT.md.

All aliases delegate to existing services; no business rules are duplicated here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.business_schemas import PaymentVerifyRequest
from api.security import Principal, current_principal, require_analytical_access
from api.services.event_bus import contract_payment_record, publish_payment_updated

router = APIRouter(tags=["contract-compatibility"])


class ContractPaymentVerify(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_id: str = Field(min_length=8, max_length=128)
    wallet_from: str = Field(min_length=42, max_length=42, pattern=r"^0x[a-fA-F0-9]{40}$")


def _contract_payment(row: dict[str, Any]) -> dict[str, Any]:
    return contract_payment_record(row)


@router.post("/payments/start")
def contract_start_payment(request: Request, background_tasks: BackgroundTasks,
                           principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    payment = request.app.state.payment_service.create_or_get(principal.user_id)
    background_tasks.add_task(publish_payment_updated, request.app.state.market_cache, principal.user_id, payment)
    if str(payment.get("status", "")).lower() == "pending":
        background_tasks.add_task(
            request.app.state.business_notification_service.notify, principal.user_id,
            event_id=f"payment-created:{payment.get('payment_id')}",
            title="Solicitud de pago creada",
            message=f"Tu solicitud de pago de {payment.get('amount_usdt', '')} USDT está lista. Completa la transferencia antes de su vencimiento.",
            business_event="payment.created", metadata={"payment_id": payment.get("payment_id"), "payment_status": "pending"},
        )
    return {
        "price": str(payment.get("amount_usdt") or ""),
        "wallet_dest": payment.get("wallet"),
        "record": _contract_payment(payment),
        "expires_at": payment.get("expires_at"),
    }


@router.post("/payments/verify")
def contract_verify_payment(body: ContractPaymentVerify, request: Request, background_tasks: BackgroundTasks,
                            principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    result = request.app.state.payment_service.verify(principal.user_id, body.record_id, body.wallet_from)
    payment = result.get("payment") or {}
    background_tasks.add_task(publish_payment_updated, request.app.state.market_cache, principal.user_id, payment)
    if str(payment.get("status", "")).lower() == "completed":
        user = request.app.state.auth_service.me(principal.user_id)
        from api.services.event_bus import publish_account_event
        background_tasks.add_task(publish_account_event, request.app.state.market_cache, principal.user_id,
                                  "plan.changed", {"user": user})
        background_tasks.add_task(
            request.app.state.business_notification_service.notify, principal.user_id,
            event_id=f"payment-completed:{payment.get('payment_id')}",
            title="Pago confirmado · Premium activado",
            message="Tu transferencia USDT ha sido confirmada y tu suscripción Premium está activa.",
            business_event="payment.completed", metadata={"payment_id": payment.get("payment_id"), "payment_status": "completed"},
        )
    elif str(payment.get("status", "")).lower() == "verified":
        background_tasks.add_task(
            request.app.state.business_notification_service.notify, principal.user_id,
            event_id=f"payment-verified:{payment.get('payment_id')}",
            title="Transferencia confirmada",
            message="La transferencia fue verificada. Estamos terminando la activación de Premium; no necesitas volver a enviar el pago.",
            business_event="payment.verified", metadata={"payment_id": payment.get("payment_id"), "payment_status": "verified"},
        )
    return {"result": result.get("result"), "record": _contract_payment(payment)}


@router.get("/payments/history")
def contract_payment_history(request: Request, limit: int = Query(default=50, ge=1, le=100),
                             principal: Principal = Depends(current_principal)) -> list[dict[str, Any]]:
    return [_contract_payment(row) for row in request.app.state.payment_service.history(principal.user_id, limit=limit)]


@router.get("/referrals")
def contract_referrals(request: Request, principal: Principal = Depends(require_analytical_access)) -> dict[str, Any]:
    summary = request.app.state.referral_service.summary(principal.user_id)
    total_premium = int(summary.get("premium_referred", 0))
    rows = summary.get("referrals", [])
    referrals = []
    for row in rows:
        referred_id = str(row.get("referred_id") or "")
        user = request.app.state.auth_repository.get_user_by_id(referred_id) if referred_id else None
        referrals.append({
            "user_id": referred_id,
            "username": (user or {}).get("username"),
            "created_at": (user or {}).get("created_at"),
            "activated_premium_at": row.get("activated_at"),
        })
    return {
        "link": summary.get("referral_link"),
        "progress": {"current": total_premium % 5, "target": 5},
        "total_referred": summary.get("total_referred", 0),
        "referrals": referrals,
        "rewards": summary.get("rewards", []),
        "reward_batches_earned": summary.get("reward_batches_earned", 0),
        "reward_days": summary.get("reward_days", 30),
    }
