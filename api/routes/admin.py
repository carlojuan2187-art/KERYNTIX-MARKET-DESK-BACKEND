"""Admin-only user, plan, moderation, support, and audit endpoints."""
import uuid
from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request

from api.business_schemas import AdminBanRequest, AdminPremiumRequest, AdminSupportReplyRequest, AdminUnbanRequest
from api.security import Principal, current_principal
from api.services.event_bus import publish_account_event, publish_payment_updated

router = APIRouter(prefix="/admin", tags=["admin"])


class ContractPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    days: int = Field(ge=1, le=3650)
    reason: str = Field(default="Activación Premium desde panel administrativo", min_length=5, max_length=500)


class ContractBanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: datetime | None = None
    reason: str = Field(min_length=5, max_length=500)


class ContractUnbanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="Desbaneo desde panel administrativo", min_length=5, max_length=500)


class StatsResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=5, max_length=500)
    confirm: bool = False


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail={"code": "ADMIN_REQUIRED", "message": "Se requieren permisos de administrador."})
    return principal


def _admin(request: Request):
    return request.app.state.admin_service


def _support(request: Request):
    return request.app.state.support_service


def _audit(request: Request, actor_id: str, action: str, target: str, details: dict | None = None) -> None:
    import uuid
    from datetime import timezone
    request.app.state.business_repository.write_audit({"event_id": uuid.uuid4().hex, "actor_id": str(actor_id), "action": action,
        "target_user_id": str(target), "details": details or {}, "created_at": datetime.now(timezone.utc)})


@router.get("/summary")
def admin_summary(request: Request, principal: Principal = Depends(require_admin)) -> dict:
    return _admin(request).summary()


@router.get("/stats")
def admin_stats_contract(request: Request, principal: Principal = Depends(require_admin)) -> dict:
    """Legacy contract alias with the field names used by the original admin panel."""
    summary = _admin(request).summary()
    return {
        "trial_users": int(summary.get("trial_active", 0)),
        "premium_users": int(summary.get("premium_active", 0)),
        "banned_users": int(summary.get("bans_active", 0)),
        "total_users": int(summary.get("users_total", 0)),
        "generated_at": summary.get("generated_at"),
    }


@router.get("/performance/baseline")
def stats_baseline(request: Request, principal: Principal = Depends(require_admin)) -> dict:
    """Current statistics cut-off and how many closed trades it counts or archives."""
    from api.services import stats_baseline as baseline
    return baseline.describe(request.app.state.business_repository, request.app.state.signal_repository)


@router.post("/performance/reset")
def stats_reset(body: StatsResetRequest, request: Request, principal: Principal = Depends(require_admin)) -> dict:
    """Start measuring statistics from zero. Closed trades are kept, only hidden from stats."""
    from api.services import stats_baseline as baseline
    if not body.confirm:
        raise HTTPException(status_code=422, detail={"code": "CONFIRMATION_REQUIRED", "message": "Confirma el reinicio de estadísticas."})
    # The reset event has one canonical audit reason, shared by the platform and every account.\n    reason = "Reinicio por optimización de estrategia"
    if not 5 <= len(reason) <= 500:
        raise HTTPException(status_code=422, detail={"code": "INVALID_REASON", "message": "Indica una razón de 5 a 500 caracteres."})
    moment = baseline.apply_reset(request.app.state.business_repository, actor_id=principal.user_id, reason=reason)
    _audit(request, principal.user_id, "stats_reset", "platform", {"reason": reason, "reset_at": moment.isoformat(), "scope": "global_and_personal"})
    return baseline.describe(request.app.state.business_repository, request.app.state.signal_repository)


@router.post("/performance/restore")
def stats_restore(request: Request, principal: Principal = Depends(require_admin)) -> dict:
    """Undo the reset: statistics count the whole closed-trade history again."""
    from api.services import stats_baseline as baseline
    baseline.clear_reset(request.app.state.business_repository)
    _audit(request, principal.user_id, "stats_restore", "platform", {})
    return baseline.describe(request.app.state.business_repository, request.app.state.signal_repository)


@router.get("/payments")
def admin_payments(request: Request, status: str | None = None,
                   limit: int = Query(default=50, ge=1, le=200),
                   principal: Principal = Depends(require_admin)) -> dict:
    allowed = {"pending", "verifying", "verified", "completed", "expired", "cancelled"}
    if status is not None and status not in allowed:
        raise HTTPException(status_code=422, detail={"code": "INVALID_PAYMENT_STATUS", "message": "Filtro de estado de pago no válido."})
    return request.app.state.payment_service.admin_history(status=status, limit=limit)


@router.post("/payments/{payment_id}/retry-activation")
def admin_retry_payment_activation(payment_id: str, request: Request, background_tasks: BackgroundTasks,
                                   principal: Principal = Depends(require_admin)) -> dict:
    result = request.app.state.payment_service.retry_activation(payment_id, principal.user_id)
    payment = result.get("payment") or {}
    target_user = str(payment.get("user_id") or "")
    if target_user:
        background_tasks.add_task(publish_payment_updated, request.app.state.market_cache, target_user, payment)
        if str(payment.get("status", "")).lower() == "completed":
            background_tasks.add_task(publish_account_event, request.app.state.market_cache, target_user,
                                      "plan.changed", {"user": request.app.state.auth_service.me(target_user)})
            background_tasks.add_task(
                request.app.state.business_notification_service.notify, target_user,
                event_id=f"payment-completed:{payment.get('payment_id')}",
                title="Pago confirmado · Premium activado",
                message="Tu transferencia USDT ha sido confirmada y tu suscripción Premium está activa.",
                business_event="payment.completed", metadata={"payment_id": payment.get("payment_id"), "payment_status": "completed"},
            )
    return result


@router.get("/users")
def list_users(request: Request, query: str | None = None, plan: str | None = None, banned: bool | None = None,
               filter: str | None = Query(default=None, pattern="^(all|trial|premium|banned)$"),
               page: int | None = Query(default=None, ge=0),
               skip: int = Query(default=0, ge=0), limit: int = Query(default=6, ge=1, le=100),
               principal: Principal = Depends(require_admin)) -> dict:
    if plan and plan.lower() not in {"free", "premium"}:
        raise HTTPException(status_code=422, detail={"code": "INVALID_PLAN_FILTER", "message": "Filtro de plan no válido."})
    if filter:
        if plan is not None or banned is not None:
            raise HTTPException(status_code=422, detail={"code": "CONFLICTING_USER_FILTERS", "message": "Usa filter o plan/banned, no ambos."})
        if filter == "premium":
            plan = "premium"
        elif filter == "banned":
            banned = True
        elif filter == "trial":
            # Query the repository's canonical active-trial selector so paging and
            # eligibility remain correct for large user collections.
            page_index = page if page is not None else (skip // limit)
            effective_skip = page_index * limit
            result = _admin(request).list_active_trial_users(query=query, skip=effective_skip, limit=limit, now=datetime.now(timezone.utc))
            result["users"] = result.pop("items")
            result["page"] = page_index
            return result
    effective_skip = page * limit if page is not None else skip
    result = _admin(request).list_users(query=query, plan=plan, banned=banned, skip=effective_skip, limit=limit)
    # Match the documented frontend contract consistently across all filters.
    result["users"] = result.pop("items")
    result["page"] = page if page is not None else (effective_skip // limit)
    return result


def _parse_iso(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


@router.get("/users/{user_id}")
def get_user(user_id: str, request: Request, principal: Principal = Depends(require_admin)) -> dict:
    return _admin(request).get_user(user_id)


@router.post("/users/{user_id}/premium")
def activate_premium(user_id: str, body: AdminPremiumRequest, request: Request, background_tasks: BackgroundTasks,
                     principal: Principal = Depends(require_admin)) -> dict:
    result = _admin(request).activate_premium(user_id, principal.user_id, days=body.days, reason=body.reason, request_id=body.request_id)
    request.app.state.referral_service.on_premium_activated(user_id, event_id=str(result.get("event_id")))
    background_tasks.add_task(publish_account_event, request.app.state.market_cache, user_id, "plan.changed",
                              {"user": request.app.state.auth_service.me(user_id)})
    background_tasks.add_task(
        request.app.state.business_notification_service.notify, user_id,
        event_id=f"admin-premium:{body.request_id}", title="Tu plan Premium ha sido actualizado",
        message=f"Un administrador actualizó tu suscripción Premium por {body.days} días.",
        business_event="plan.admin_activation", metadata={"request_id": body.request_id, "days": body.days},
    )
    return result


@router.post("/users/{user_id}/plan")
def contract_activate_plan(user_id: str, body: ContractPlanRequest, request: Request, background_tasks: BackgroundTasks,
                           idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128),
                           principal: Principal = Depends(require_admin)) -> dict:
    # PlanService applies extensions in chunks of at most 365 days. Stable
    # per-chunk keys make a retried 3650-day request resume without double-crediting.
    # The client must supply the key; a generated key would make network retries unsafe.
    request_id = idempotency_key.strip()
    if not 8 <= len(request_id) <= 128:
        raise HTTPException(status_code=422, detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "Idempotency-Key debe tener entre 8 y 128 caracteres."})
    remaining = body.days
    part = 0
    results = []
    while remaining > 0:
        part += 1
        chunk = min(365, remaining)
        results.append(_admin(request).activate_premium(
            user_id, principal.user_id, days=chunk, reason=body.reason,
            request_id=f"{request_id}:part:{part}"))
        remaining -= chunk
    result = dict(results[-1])
    result["duration_days_requested"] = body.days
    result["event_ids"] = [item.get("event_id") for item in results]
    result["activation_applied"] = any(bool(item.get("activation_applied")) for item in results)
    request.app.state.referral_service.on_premium_activated(user_id, event_id=str(result.get("event_id")))
    background_tasks.add_task(publish_account_event, request.app.state.market_cache, user_id, "plan.changed",
                              {"user": request.app.state.auth_service.me(user_id)})
    background_tasks.add_task(
        request.app.state.business_notification_service.notify, user_id,
        event_id=f"admin-plan:{request_id}", title="Tu plan Premium ha sido actualizado",
        message=f"Un administrador actualizó tu suscripción Premium por {body.days} días.",
        business_event="plan.admin_activation", metadata={"request_id": request_id, "days": body.days},
    )
    # The documented response is an updated User. Keep the plan-service snapshot
    # as additive metadata to help the admin UI render the resulting entitlement.
    return {**_admin(request).get_user(user_id), **result}


@router.post("/users/{user_id}/ban")
def ban_user(user_id: str, body: AdminBanRequest, request: Request, background_tasks: BackgroundTasks,
             principal: Principal = Depends(require_admin)) -> dict:
    result = _admin(request).ban_user(user_id, principal.user_id, days=body.days, reason=body.reason)
    background_tasks.add_task(publish_account_event, request.app.state.market_cache, user_id, "ban.changed",
                              {"user_id": user_id, "banned": True, "ban_until": result.get("ban_until"), "reason": body.reason})
    return result


@router.post("/users/{user_id}/unban")
def unban_user(user_id: str, body: AdminUnbanRequest, request: Request, background_tasks: BackgroundTasks,
               principal: Principal = Depends(require_admin)) -> dict:
    result = _admin(request).unban_user(user_id, principal.user_id, reason=body.reason)
    background_tasks.add_task(publish_account_event, request.app.state.market_cache, user_id, "ban.changed",
                              {"user_id": user_id, "banned": False, "ban_until": None, "reason": body.reason})
    return result


@router.patch("/users/{user_id}/ban")
def contract_ban_user(user_id: str, body: ContractBanRequest, request: Request, background_tasks: BackgroundTasks,
                      principal: Principal = Depends(require_admin)) -> dict:
    result = _admin(request).ban_user_until(user_id, principal.user_id, until=body.until, reason=body.reason)
    background_tasks.add_task(publish_account_event, request.app.state.market_cache, user_id, "ban.changed",
                              {"user_id": user_id, "banned": True, "ban_until": result.get("ban_until"), "reason": body.reason})
    return result


@router.patch("/users/{user_id}/unban")
def contract_unban_user(user_id: str, request: Request, background_tasks: BackgroundTasks,
                        body: ContractUnbanRequest | None = None,
                        principal: Principal = Depends(require_admin)) -> dict:
    reason = body.reason if body is not None else "Desbaneo desde panel administrativo"
    result = _admin(request).unban_user(user_id, principal.user_id, reason=reason)
    background_tasks.add_task(publish_account_event, request.app.state.market_cache, user_id, "ban.changed",
                              {"user_id": user_id, "banned": False, "ban_until": None, "reason": reason})
    return result


@router.get("/audit")
def audit_log(request: Request, limit: int = Query(default=100, ge=1, le=200), before: datetime | None = None,
              principal: Principal = Depends(require_admin)) -> dict:
    return _admin(request).audit(limit=limit, before=before)


@router.get("/support/tickets")
def admin_list_tickets(request: Request, status: str | None = None, limit: int = Query(default=50, ge=1, le=100), before: datetime | None = None,
                       principal: Principal = Depends(require_admin)) -> dict:
    return _support(request).list_admin(status=status, limit=limit, before=before)


@router.post("/support/tickets/{ticket_id}/reply")
def admin_reply_ticket(ticket_id: str, body: AdminSupportReplyRequest, request: Request,
                       principal: Principal = Depends(require_admin)) -> dict:
    result = _support(request).message(ticket_id, principal.user_id, "admin", body.message, admin=True, close=body.close or body.status == "closed", status_override=body.status)
    _audit(request, principal.user_id, "support_reply", str(ticket_id), {"close": body.close, "status": body.status})
    return result
