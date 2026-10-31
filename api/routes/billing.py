"""User-facing Premium checkout and payment verification endpoints."""
from __future__ import annotations

import os
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request

from api.business_schemas import PaymentVerifyRequest
from api.security import Principal, current_principal
from api.services.payments import PaymentService, amount_to_string
from api.services.event_bus import publish_payment_updated, publish_account_event

router = APIRouter(prefix="/billing", tags=["billing"])


def _payments(request: Request) -> PaymentService:
    return request.app.state.payment_service


@router.get("/plans")
def list_billable_plans() -> dict:
    """Public pricing/configuration; this response never claims payment is configured if it isn't."""
    try:
        price = Decimal(os.getenv("PREMIUM_PRICE_USDT", "10.00"))
        valid_price = price.is_finite() and price > 0
    except Exception:
        price, valid_price = Decimal("0"), False
    from api.services.payments import BscRpcVerifier
    verifier = BscRpcVerifier()
    wallet = os.getenv("PAYMENT_WALLET_ADDRESS", "").strip().lower()
    configured = verifier.is_configured() and valid_price
    return {"plans": [{"plan": "premium", "price_usdt": str(price.quantize(Decimal('0.001'))) if valid_price else None,
                       "duration_days": 30, "network": "BNB Smart Chain (BEP20)", "token": "USDT",
                       "wallet": wallet if configured else None, "auto_verification": configured}],
            "payment_configured": configured}


@router.get("/payments/current")
def current_payment(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    payment = _payments(request).current(principal.user_id)
    return {"payment": payment}


@router.get("/payments/history")
def payment_history(request: Request, limit: int = Query(default=30, ge=1, le=100), principal: Principal = Depends(current_principal)) -> dict:
    return {"items": _payments(request).history(principal.user_id, limit=limit)}


@router.post("/payments")
def create_payment(request: Request, background_tasks: BackgroundTasks,
                   principal: Principal = Depends(current_principal)) -> dict:
    payment = _payments(request).create_or_get(principal.user_id)
    background_tasks.add_task(publish_payment_updated, request.app.state.market_cache, principal.user_id, payment)
    request.app.state.business_notification_service  # shared durable plan/payment outbox
    if str(payment.get("status", "")).lower() == "pending":
        background_tasks.add_task(
            request.app.state.business_notification_service.notify, principal.user_id,
            event_id=f"payment-created:{payment.get('payment_id')}",
            title="Solicitud de pago creada",
            message=f"Tu solicitud de pago de {payment.get('amount_usdt', '')} USDT está lista. Completa la transferencia antes de su vencimiento.",
            business_event="payment.created", metadata={"payment_id": payment.get("payment_id"), "payment_status": "pending"},
        )
    return {"payment": payment}


@router.post("/payments/{payment_id}/verify")
def verify_payment(payment_id: str, body: PaymentVerifyRequest, request: Request, background_tasks: BackgroundTasks,
                   principal: Principal = Depends(current_principal)) -> dict:
    result = _payments(request).verify(principal.user_id, payment_id, body.sender_address)
    payment = result.get("payment") or {}
    background_tasks.add_task(publish_payment_updated, request.app.state.market_cache, principal.user_id, payment)
    if str(payment.get("status", "")).lower() == "completed":
        background_tasks.add_task(publish_account_event, request.app.state.market_cache, principal.user_id,
                                  "plan.changed", {"user": request.app.state.auth_service.me(principal.user_id)})
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
    return result
