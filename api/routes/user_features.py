"""User watchlist, alert preferences, user price alerts and notification inbox."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from api.features_schemas import AlertPreferencesPatch, PriceAlertCreate, PriceAlertPatch, WatchlistAddRequest
from api.security import Principal, require_analytical_access
from api.services.user_features import AlertPreferencesService, NotificationService, PriceAlertService, WatchlistService

router = APIRouter(tags=["user-features"])


def _watchlist(request: Request) -> WatchlistService:
    return request.app.state.watchlist_service


def _preferences(request: Request) -> AlertPreferencesService:
    return request.app.state.alert_preferences_service


def _price_alerts(request: Request) -> PriceAlertService:
    return request.app.state.price_alert_service


def _notifications(request: Request) -> NotificationService:
    return request.app.state.notification_service


@router.get("/watchlist")
async def get_watchlist(request: Request, principal: Principal = Depends(require_analytical_access)) -> dict:
    return await _watchlist(request).list(principal.user_id)


@router.post("/watchlist", status_code=status.HTTP_201_CREATED)
async def add_watchlist_symbol(body: WatchlistAddRequest, request: Request,
                               principal: Principal = Depends(require_analytical_access)) -> dict:
    return await _watchlist(request).add(principal.user_id, body.symbol)


@router.delete("/watchlist/{symbol}")
async def remove_watchlist_symbol(symbol: str, request: Request,
                                  principal: Principal = Depends(require_analytical_access)) -> dict:
    return await _watchlist(request).remove(principal.user_id, symbol)


@router.get("/account/alert-preferences")
def get_alert_preferences(request: Request, principal: Principal = Depends(require_analytical_access)) -> dict:
    return _preferences(request).get(principal.user_id)


@router.patch("/account/alert-preferences")
def patch_alert_preferences(body: AlertPreferencesPatch, request: Request,
                            principal: Principal = Depends(require_analytical_access)) -> dict:
    return _preferences(request).patch(principal.user_id, body.model_dump(exclude_unset=True))


@router.get("/alerts/price")
async def list_price_alerts(request: Request, enabled_only: bool = Query(default=False),
                            principal: Principal = Depends(require_analytical_access)) -> list[dict]:
    return await _price_alerts(request).list(principal.user_id, enabled_only=enabled_only)


@router.post("/alerts/price", status_code=status.HTTP_201_CREATED)
async def create_price_alert(body: PriceAlertCreate, request: Request,
                             principal: Principal = Depends(require_analytical_access)) -> dict:
    return await _price_alerts(request).create(principal.user_id, body.model_dump())


@router.patch("/alerts/price/{alert_id}")
def patch_price_alert(alert_id: str, body: PriceAlertPatch, request: Request,
                      principal: Principal = Depends(require_analytical_access)) -> dict:
    return _price_alerts(request).patch(principal.user_id, alert_id, body.model_dump(exclude_unset=True))


@router.delete("/alerts/price/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_price_alert(alert_id: str, request: Request,
                       principal: Principal = Depends(require_analytical_access)) -> Response:
    _price_alerts(request).delete(principal.user_id, alert_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/notifications")
def list_notifications(request: Request, unread_only: bool = Query(default=False),
                       limit: int = Query(default=50, ge=1, le=100), before: datetime | None = Query(default=None),
                       principal: Principal = Depends(require_analytical_access)) -> dict:
    return _notifications(request).list(principal.user_id, unread_only=unread_only, limit=limit, before=before)


@router.post("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, request: Request,
                           principal: Principal = Depends(require_analytical_access)) -> dict:
    return _notifications(request).mark_read(principal.user_id, notification_id)


@router.post("/notifications/read-all")
def mark_all_notifications_read(request: Request, principal: Principal = Depends(require_analytical_access)) -> dict:
    return _notifications(request).mark_all_read(principal.user_id)


@router.delete("/notifications")
def delete_all_notifications(request: Request,
                             principal: Principal = Depends(require_analytical_access)) -> dict:
    return _notifications(request).delete_all(principal.user_id)


@router.delete("/notifications/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification(notification_id: str, request: Request,
                        principal: Principal = Depends(require_analytical_access)) -> Response:
    _notifications(request).delete(principal.user_id, notification_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
