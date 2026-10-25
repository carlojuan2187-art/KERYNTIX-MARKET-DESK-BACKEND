"""Referral dashboard and reward summary."""
from fastapi import APIRouter, Request
from api.security import Principal, current_principal
from fastapi import Depends

router = APIRouter(prefix="/referrals", tags=["referrals"])


@router.get("/summary")
def referral_summary(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    return request.app.state.referral_service.summary(principal.user_id)
