"""Authenticated endpoints for browser Web Push registration."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from api.security import Principal, require_analytical_access
from api.services.web_push import WebPushService
from api.web_push_schemas import WebPushSubscriptionCreate

router = APIRouter(tags=["web-push"])


def _service(request: Request) -> WebPushService:
    return request.app.state.web_push_service


@router.get("/push/vapid-public-key")
def get_vapid_public_key(request: Request) -> dict[str, str]:
    """Return only the public VAPID key. Private key material is never exposed."""
    return _service(request).public_key()


@router.get("/push/subscriptions")
def list_push_subscriptions(request: Request, principal: Principal = Depends(require_analytical_access)) -> dict:
    return _service(request).list(principal.user_id)


@router.post("/push/subscriptions", status_code=status.HTTP_201_CREATED)
def register_push_subscription(body: WebPushSubscriptionCreate, request: Request,
                               principal: Principal = Depends(require_analytical_access)) -> dict:
    return _service(request).register(
        principal.user_id, body.model_dump(by_alias=False),
        user_agent=request.headers.get("user-agent"),
    )


@router.delete("/push/subscriptions/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_push_subscription(subscription_id: str, request: Request,
                             principal: Principal = Depends(require_analytical_access)) -> Response:
    _service(request).delete(principal.user_id, subscription_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
