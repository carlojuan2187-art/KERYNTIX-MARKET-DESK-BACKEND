"""Account restriction, Telegram binding, and session-management endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from api.auth_schemas import SessionRecordResponse, TelegramLinkResponse, UserResponse
from api.security import Principal, current_principal
from api.services.auth import AuthService

router = APIRouter(tags=["account"])


def _service(request: Request) -> AuthService:
    return request.app.state.auth_service


@router.get("/account/restriction")
def account_restriction(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    return _service(request).account_restriction(principal.user_id)


@router.post("/account/telegram", response_model=TelegramLinkResponse)
def create_telegram_link(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    return _service(request).create_telegram_link_request(principal.user_id)


@router.delete("/account/telegram", response_model=UserResponse)
def unlink_telegram(request: Request, principal: Principal = Depends(current_principal)) -> dict:
    return _service(request).unlink_telegram(principal.user_id)


@router.get("/sessions", response_model=list[SessionRecordResponse])
def list_sessions(request: Request, principal: Principal = Depends(current_principal)) -> list[dict]:
    current_session = principal.claims.get("sid")
    return _service(request).sessions(principal.user_id, str(current_session) if current_session else None)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session(session_id: str, request: Request, principal: Principal = Depends(current_principal)) -> Response:
    _service(request).revoke_session(principal.user_id, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
