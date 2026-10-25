"""Account plan and entitlement endpoints. These remain available after expiry."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from api.security import Principal, current_principal
from api.services.plans import PlanService

router = APIRouter(prefix="/account", tags=["plans"])


def _plans(request: Request) -> PlanService:
    return request.app.state.plan_service


@router.get("/plan")
def get_current_plan(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    """Return current persisted plan and effective status calculated against UTC now."""
    return _plans(request).snapshot(principal.user_id)


@router.get("/entitlements")
def get_current_entitlements(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    """Return the central feature map used by the frontend for gating and lock screens."""
    return _plans(request).account_entitlements(principal.user_id)
