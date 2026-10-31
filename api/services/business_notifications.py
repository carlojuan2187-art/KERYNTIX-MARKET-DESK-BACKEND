"""Durable transactional notifications for payments, plans and referrals.

Business notifications reuse the same notification document and per-channel outboxes
as market alerts, so retries survive restarts and are deduplicated by (user_id,event_id).
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from api.repositories.auth import AuthRepository
from api.repositories.user_features import UserFeatureRepository
from api.services.user_features import alert_preferences
from api.services.localization import localize_notification

logger = logging.getLogger("ftrade.business_notifications")
UTC = timezone.utc
NOTIFICATION_CHANNEL = "notifications:events:v1"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return moment.isoformat().replace("+00:00", "Z")
    return str(value or "")


class BusinessNotificationService:
    """Enqueue idempotent user notifications for durable business state changes.

    `enqueue` is synchronous so it can be called safely by domain services that also
    run in workers. Delivery is asynchronous and delegated to the existing Telegram
    and Web Push outbox workers.
    """

    def __init__(self, auth: AuthRepository, features: UserFeatureRepository, cache: Any | None = None) -> None:
        self.auth = auth
        self.features = features
        self.cache = cache

    def enqueue(self, user_id: str, *, event_id: str, title: str, message: str,
                business_event: str, metadata: Mapping[str, Any] | None = None,
                now: datetime | None = None, kind: str = "plans") -> dict[str, Any]:
        target = str(user_id or "").strip()
        stable_event_id = str(event_id or "").strip()
        if not target or not stable_event_id or len(stable_event_id) > 240:
            return {"created": False, "reason": "INVALID_NOTIFICATION_IDENTITY", "notification": None}
        user = self.auth.get_user_by_id(target)
        if not user:
            return {"created": False, "reason": "USER_NOT_FOUND", "notification": None}

        prefs = alert_preferences(user)
        # Transactional business notices use the user's existing Plans notification
        # preference, rather than inventing a parallel preference model.
        if not prefs.get("on", True) or not prefs.get("plans", True):
            return {"created": False, "reason": "PREFERENCES_DISABLED", "notification": None}

        current = now or _now()
        in_app = bool(prefs.get("in_app", True))
        chat_id = str(user.get("telegram_chat_id") or "")
        use_telegram = bool(prefs.get("telegram", True) and chat_id)
        try:
            use_push = bool(prefs.get("web_push", False)
                            and self.features.active_push_subscription_count(target) > 0)
        except Exception:
            logger.warning("No se pudo comprobar Web Push para aviso transaccional", exc_info=True)
            use_push = False
        if not (in_app or use_telegram or use_push):
            return {"created": False, "reason": "NO_CHANNEL_ENABLED", "notification": None}

        safe_metadata = dict(metadata or {})
        safe_metadata.update({"transactional": True, "business_event": str(business_event)[:80]})
        clean_title, clean_message = localize_notification(
            str(title).strip(), str(message).strip(), user.get("language", "en")
        )
        clean_title, clean_message = clean_title[:160], clean_message[:2000]
        document: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "user_id": target,
            "event_id": stable_event_id,
            "kind": kind,
            "title": clean_title,
            "message": clean_message,
            "symbol": None,
            "metadata": safe_metadata,
            "created_at": current,
            "read_at": None,
            "in_app": in_app,
        }
        if use_telegram:
            document["telegram_delivery"] = {
                "status": "pending", "chat_id": chat_id,
                "text": f"{clean_title}\n\n{clean_message}", "attempts": 0,
                "next_attempt_at": current, "created_at": current, "updated_at": current,
            }
        if use_push:
            document["web_push_delivery"] = {
                "status": "pending", "attempts": 0, "endpoint_results": {},
                "next_attempt_at": current, "created_at": current, "updated_at": current,
            }
        saved, created = self.features.create_notification(document)
        return {"created": bool(created), "reason": "ENQUEUED" if created else "DUPLICATE_EVENT",
                "notification": saved, "queued_channels": [name for name, enabled in (
                    ("in_app", in_app), ("telegram", use_telegram), ("web_push", use_push)) if enabled]}

    async def notify(self, user_id: str, *, event_id: str, title: str, message: str,
                     business_event: str, metadata: Mapping[str, Any] | None = None,
                     now: datetime | None = None, kind: str = "plans") -> dict[str, Any]:
        """Enqueue and publish a best-effort in-app event; REST remains canonical."""
        result = await asyncio.to_thread(
            self.enqueue, user_id, event_id=event_id, title=title, message=message,
            business_event=business_event, metadata=metadata, now=now, kind=kind,
        )
        notification = result.get("notification")
        if self.cache is not None and isinstance(notification, Mapping) and notification.get("in_app"):
            try:
                await self.cache.publish_json(NOTIFICATION_CHANNEL, {
                    "event": "notification.created", "user_id": str(user_id),
                    "notification": {
                        "id": str(notification.get("id") or ""),
                        "event_id": str(notification.get("event_id") or ""),
                        "kind": str(notification.get("kind") or kind),
                        "title": str(notification.get("title") or title),
                        "message": str(notification.get("message") or message),
                        "symbol": notification.get("symbol"),
                        "created_at": _iso(notification.get("created_at")),
                    },
                })
            except Exception as exc:
                # Durable inbox and channel outboxes are already persisted.
                logger.warning("No se pudo publicar notificación de negocio (%s)", type(exc).__name__)
        return {key: value for key, value in result.items() if key != "notification"} | {
            "notification_id": str(notification.get("id")) if isinstance(notification, Mapping) else None
        }
