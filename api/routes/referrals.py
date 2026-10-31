"""Referral dashboard and reward summary."""
from fastapi import APIRouter, Request
from api.security import Principal, require_analytical_access
from fastapi import Depends

router = APIRouter(prefix="/referrals", tags=["referrals"])


@router.get("/summary")
def referral_summary(request: Request, principal: Principal = Depends(require_analytical_access)) -> dict:
    return request.app.state.referral_service.summary(principal.user_id)
