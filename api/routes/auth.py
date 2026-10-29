"""Public authentication and current-user endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status

from api.auth_schemas import (
    AuthResendRequest, PhoneChallengeRequest, PhoneChallengeResponse,
    RefreshRequest, RefreshResponse, TelegramCheckResponse, UserPatchRequest, UserResponse,
    VerifyCodeRequest, VerifyCodeResponse,
)
from api.security import Principal, current_principal
from api.services.auth import ACCESS_COOKIE_NAME, AuthService, refresh_in_body_enabled
from api.services.telegram import TelegramDeliveryError

router = APIRouter(prefix="/auth", tags=["authentication"])


def _service(request: Request) -> AuthService:
    return request.app.state.auth_service


@router.post("/phone", response_model=PhoneChallengeResponse)
def request_phone_challenge(body: PhoneChallengeRequest, request: Request) -> dict:
    requester_ip = request.client.host if request.client else None
    return _service(request).start_phone_challenge(body.phone, ref_code=body.ref_code, requester_ip=requester_ip)


@router.get("/telegram-check", response_model=TelegramCheckResponse)
def telegram_check(token: str, request: Request) -> dict:
    if len(token) < 20 or len(token) > 128:
        raise HTTPException(status_code=422, detail={"code": "INVALID_TOKEN", "message": "Token de solicitud inválido."})
    return _service(request).telegram_check(token)


@router.post("/verify", response_model=VerifyCodeResponse)
def verify_code(body: VerifyCodeRequest, request: Request, response: Response) -> dict:
    user, access_token, refresh_token, is_new = _service(request).verify_code(body.phone, body.code, request=request)
    _service(request).set_refresh_cookie(response, refresh_token)
    return {"access_token": access_token, "user": user, "is_new": is_new,
            "refresh_token": refresh_token if refresh_in_body_enabled() else None}


@router.post("/resend")
def resend_code(body: AuthResendRequest, request: Request) -> dict:
    return _service(request).resend_code(body.token)


@router.post("/refresh", response_model=RefreshResponse)
def refresh_session(request: Request, response: Response, body: RefreshRequest | None = None,
                    refresh_token: str | None = Cookie(default=None, alias=ACCESS_COOKIE_NAME)) -> dict:
    # The HttpOnly cookie is preferred; the body token is a fallback for browsers
    # that drop third-party cookies (Safari/ITP, Brave, in-app browsers).
    access_token, new_refresh, expires_at = _service(request).refresh(refresh_token, fallback_token=body.refresh_token if body else None)
    _service(request).set_refresh_cookie(response, new_refresh, expires_at=expires_at)
    return {"access_token": access_token, "refresh_token": new_refresh if refresh_in_body_enabled() else None}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, body: RefreshRequest | None = None,
           refresh_token: str | None = Cookie(default=None, alias=ACCESS_COOKIE_NAME)) -> Response:
    revoked = _service(request).logout(refresh_token) or (bool(body and body.refresh_token) and _service(request).logout(body.refresh_token))
    _service(request).clear_refresh_cookie(response)
    if not revoked:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "La sesión de renovación no existe o ya fue revocada."})
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserResponse)
def get_me(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    return _service(request).me(principal.user_id)


@router.patch("/me", response_model=UserResponse)
def patch_me(body: UserPatchRequest, request: Request, principal: Principal = Depends(current_principal)) -> dict:
    return _service(request).patch_me(principal.user_id, body.model_dump(exclude_unset=True))
