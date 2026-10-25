"""Authenticated user support-ticket endpoints."""
from datetime import datetime
from fastapi import APIRouter, Depends, Query, Request

from api.business_schemas import SupportCreateRequest, SupportMessageRequest
from api.security import Principal, current_principal

router = APIRouter(prefix="/support", tags=["support"])


def _service(request: Request):
    return request.app.state.support_service


@router.post("/tickets", status_code=201)
def create_ticket(body: SupportCreateRequest, request: Request, principal: Principal = Depends(current_principal)) -> dict:
    return _service(request).create(principal.user_id, body.subject, body.message)


@router.get("/tickets")
def list_tickets(request: Request, status: str | None = None, limit: int = Query(default=50, ge=1, le=100), before: datetime | None = None,
                principal: Principal = Depends(current_principal)) -> dict:
    return _service(request).list_mine(principal.user_id, status=status, limit=limit, before=before)


@router.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str, request: Request, principal: Principal = Depends(current_principal)) -> dict:
    return _service(request).get_mine(principal.user_id, ticket_id)


@router.post("/tickets/{ticket_id}/messages")
def add_ticket_message(ticket_id: str, body: SupportMessageRequest, request: Request,
                       principal: Principal = Depends(current_principal)) -> dict:
    return _service(request).message(ticket_id, principal.user_id, "user", body.message)
