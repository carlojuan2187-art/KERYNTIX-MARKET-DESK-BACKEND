"""Centralized plan entitlement, extension, and expiry-notification logic."""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from fastapi import HTTPException

from api.repositories.auth import AuthRepository
from api.services.auth import is_admin_user
from api.services.telegram import TelegramBotAPI
from api.services.localization import localize_notification
from domain.plans import as_utc, evaluate_access, iso_utc

logger = logging.getLogger("ftrade.plans")
UTC = timezone.utc
ALLOWED_PLAN_SOURCES = {"payment", "admin", "referral", "support", "migration", "manual"}


def _now() -> datetime:
    return datetime.now(UTC)


class PlanService:
    """Use this service for every future path that changes a user's subscription."""

    def __init__(self, repository: AuthRepository) -> None:
        self.repository = repository

    def snapshot(self, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        user = self.repository.get_user_by_id(str(user_id))
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        current = as_utc(now or _now()) or _now()
        access = evaluate_access(user, now=current, is_admin=is_admin_user(user))
        end = as_utc(user.get("plan_end")) if access["premium_active"] else as_utc(user.get("trial_end")) if access["trial_active"] else None
        seconds_left = max(0, int((end - current).total_seconds())) if end else None
        return {
            "user_id": str(user_id),
            "plan": access["plan"],
            "state": access["status"],
            "allowed": access["allowed"],
            "reason": access["reason"],
            "trial_end": access["trial_end"],
            "plan_end": access["plan_end"],
            "access_until": access["access_until"],
            "seconds_until_access_end": seconds_left,
            "premium_active": access["premium_active"],
            "trial_active": access["trial_active"],
            "ban_active": access["ban_active"],
            "ban_until": access["ban_until"],
            "features": access["features"],
            "checked_at": iso_utc(current),
        }

    def activate_premium(
        self,
        user_id: str,
        *,
        days: int = 30,
        source: str,
        actor_id: str | None = None,
        reference: str | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Extend Premium atomically, preserving any still-active paid period.

        Payment, admin, and referral flows must call this shared method rather
        than directly mutating `plan_end`. `idempotency_key` should be a stable
        payment/event identifier when the caller processes a retryable event.
        """
        current = as_utc(now or _now()) or _now()
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise HTTPException(status_code=422, detail={"code": "INVALID_PLAN_DURATION", "message": "La extensión debe estar entre 1 y 365 días."})
        normalized_source = str(source).strip().lower()
        if normalized_source not in ALLOWED_PLAN_SOURCES:
            raise HTTPException(status_code=422, detail={"code": "INVALID_PLAN_SOURCE", "message": "El origen de la activación no está permitido."})
        if not self.repository.get_user_by_id(str(user_id)):
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        if idempotency_key is not None:
            event_id = str(idempotency_key).strip()
        else:
            event_id = f"{normalized_source}:{reference or uuid.uuid4().hex}".strip()
        if not event_id or len(event_id) > 200:
            raise HTTPException(status_code=422, detail={"code": "INVALID_IDEMPOTENCY_KEY", "message": "La clave de idempotencia no es válida."})
        event = {
            "event_id": event_id,
            "source": normalized_source,
            "actor_id": str(actor_id) if actor_id is not None else None,
            "reference": str(reference) if reference is not None else None,
            "duration_days": days,
            "created_at": current,
        }
        user, applied = self.repository.extend_premium_plan(str(user_id), days, event, now=current)
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        result = self.snapshot(str(user_id), now=current)
        result["activation_applied"] = applied
        result["event_id"] = event_id
        return result

    def account_entitlements(self, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
        return self.snapshot(user_id, now=now)


class PlanMaintenanceService:
    """Sends one-time upcoming/expired notices, with durable deduplication leases."""

    def __init__(self, repository: AuthRepository, telegram: TelegramBotAPI | Any, *,
                 business_notifications: Any | None = None,
                 warning_hours: int | None = None, lease_seconds: int | None = None) -> None:
        self.repository = repository
        self.telegram = telegram
        self.business_notifications = business_notifications
        self.warning_hours = max(1, min(168, warning_hours if warning_hours is not None else int(os.getenv("PLAN_NOTICE_WINDOW_HOURS", "24"))))
        self.lease_seconds = max(15, min(600, lease_seconds if lease_seconds is not None else int(os.getenv("PLAN_NOTICE_LEASE_SECONDS", "90"))))

    @staticmethod
    def _notice_for(user: Mapping[str, Any], *, now: datetime, since: datetime, until: datetime) -> list[tuple[str, datetime, str]]:
        notices: list[tuple[str, datetime, str]] = []
        trial_end = as_utc(user.get("trial_end"))
        plan_end = as_utc(user.get("plan_end"))
        premium = str(user.get("plan") or "free").lower() == "premium"
        premium_active = premium and plan_end is not None and plan_end > now
        trial_active = trial_end is not None and trial_end > now
        if trial_end and since <= trial_end <= until:
            if trial_end > now and not premium_active:
                notices.append(("trial_ending", trial_end, "Tu periodo de prueba de KERYNTIX MARKET DESK termina pronto. Revisa tus opciones de suscripción para mantener el acceso."))
            elif trial_end <= now and not premium_active:
                notices.append(("trial_expired", trial_end, "Tu periodo de prueba de KERYNTIX MARKET DESK ha terminado. Puedes revisar los planes disponibles desde tu cuenta."))
        if premium and plan_end and since <= plan_end <= until:
            if plan_end > now:
                notices.append(("premium_ending", plan_end, "Tu suscripción Premium de KERYNTIX MARKET DESK termina pronto. Revisa tu cuenta para renovarla."))
            else:
                message = ("Tu periodo Premium de KERYNTIX MARKET DESK ha terminado, pero tu periodo de prueba sigue activo hasta "
                           f"{iso_utc(trial_end)}. Puedes revisar los planes disponibles desde tu cuenta.") if trial_active else (
                           "Tu periodo Premium de KERYNTIX MARKET DESK ha terminado. Puedes revisar las opciones de renovación desde tu cuenta.")
                notices.append(("premium_expired", plan_end, message))
        return notices

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = as_utc(now or _now()) or _now()
        since = current - timedelta(days=2)
        until = current + timedelta(hours=self.warning_hours)
        candidates = self.repository.list_users_for_plan_maintenance(since=since, until=until, limit=2000)
        counts = {"status": "ok", "checked_at": iso_utc(current), "candidates": len(candidates), "sent": 0, "queued": 0, "suppressed": 0, "failed": 0, "skipped_unlinked": 0, "deduplicated": 0}
        for user in candidates:
            chat_id = user.get("telegram_chat_id")
            if not chat_id and self.business_notifications is None:
                counts["skipped_unlinked"] += 1
                continue
            user_id = str(user.get("user_id") or "")
            for kind, boundary, message in self._notice_for(user, now=current, since=since, until=until):
                claim = self.repository.claim_plan_notice(
                    user_id, kind, boundary, now=current,
                    lease_until=current + timedelta(seconds=self.lease_seconds),
                )
                if claim is None:
                    counts["deduplicated"] += 1
                    continue
                dedupe_key, claim_id = claim
                if self.business_notifications is not None:
                    try:
                        title = {
                            "trial_ending": "Tu periodo de prueba termina pronto",
                            "trial_expired": "Tu periodo de prueba ha terminado",
                            "premium_ending": "Tu suscripción Premium termina pronto",
                            "premium_expired": "Tu suscripción Premium ha terminado",
                        }.get(kind, "Actualización de tu plan KERYNTIX MARKET DESK")
                        queued = self.business_notifications.enqueue(
                            user_id, event_id=f"plan-notice:{dedupe_key}", title=title, message=message,
                            business_event=f"plan.{kind}", metadata={"notice_kind": kind, "boundary": iso_utc(boundary)}, now=current,
                        )
                        # The notification document and channel outboxes are durable at
                        # this point. A disabled preference is a terminal suppression.
                        self.repository.finish_plan_notice(dedupe_key, claim_id, sent=True, now=current)
                        if queued.get("created"):
                            counts["queued"] += 1
                        elif queued.get("reason") in {"PREFERENCES_DISABLED", "NO_CHANNEL_ENABLED"}:
                            counts["suppressed"] += 1
                        else:
                            counts["deduplicated"] += 1
                    except Exception as exc:
                        self.repository.finish_plan_notice(dedupe_key, claim_id, sent=False, now=current, error=type(exc).__name__)
                        counts["failed"] += 1
                        logger.warning("No se pudo encolar aviso de plan a %s (%s)", user_id, type(exc).__name__)
                    continue
                try:
                    _title = {
                        "trial_ending": "Tu periodo de prueba termina pronto",
                        "trial_expired": "Tu periodo de prueba ha terminado",
                        "premium_ending": "Tu suscripción Premium termina pronto",
                        "premium_expired": "Tu suscripción Premium ha terminado",
                    }.get(kind, "Actualización de tu plan KERYNTIX MARKET DESK")
                    _title, localized_message = localize_notification(_title, message, user.get("language", "en"))
                    self.telegram.send_message(str(chat_id), localized_message)
                except Exception as exc:
                    self.repository.finish_plan_notice(dedupe_key, claim_id, sent=False, now=current, error=type(exc).__name__)
                    counts["failed"] += 1
                    logger.warning("No se pudo enviar aviso de plan a %s (%s)", user_id, type(exc).__name__)
                else:
                    self.repository.finish_plan_notice(dedupe_key, claim_id, sent=True, now=current)
                    counts["sent"] += 1
        if counts["failed"]:
            counts["status"] = "degraded"
        return counts
