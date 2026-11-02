"""Idempotent referral attribution and Premium reward processing."""
from __future__ import annotations

import os
import uuid
from urllib.parse import quote, urlsplit
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from api.repositories.auth import AuthRepository
from api.repositories.business import InMemoryBusinessRepository, MongoBusinessRepository
from api.services.plans import PlanService

UTC = timezone.utc
REWARD_REFERRALS = 5
REWARD_DAYS = 30


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


class ReferralService:
    def __init__(self, auth_repository: AuthRepository, business_repository: Any, plan_service: PlanService,
                 *, business_notifications: Any | None = None) -> None:
        self.auth_repository = auth_repository
        self.repository = business_repository
        self.plan_service = plan_service
        self.business_notifications = business_notifications

    def on_premium_activated(self, referred_user_id: str, *, event_id: str, now: datetime | None = None) -> dict[str, Any]:
        """Record a referred user once after they have Premium, then process rewards.

        It deliberately does not treat trial creation or merely registering as a
        valid referral. A stable referred_id unique index prevents repeated credit.
        """
        current = now or _now()
        referred = self.auth_repository.get_user_by_id(str(referred_user_id))
        if not referred or str(referred.get("plan", "free")).lower() != "premium":
            return {"recorded": False, "reason": "PREMIUM_NOT_ACTIVE"}
        referrer_id = str(referred.get("referred_by") or "")
        if not referrer_id or referrer_id == str(referred_user_id):
            return {"recorded": False, "reason": "NO_VALID_REFERRER"}
        if not self.auth_repository.get_user_by_id(referrer_id):
            return {"recorded": False, "reason": "REFERRER_NOT_FOUND"}
        referral_id = f"referral:{referred_user_id}"
        record, inserted = self.repository.record_referral({
            "referral_id": referral_id,
            "referrer_id": referrer_id,
            "referred_id": str(referred_user_id),
            "activation_event_id": str(event_id),
            "activated_plan": "premium",
            "activated_at": current,
            "reward_applied": False,
        })
        # Enqueue on every retry with a stable key. This also repairs the crash window
        # where the referral record was stored but its notification was not yet created.
        if self.business_notifications is not None:
            try:
                self.business_notifications.enqueue(
                    referrer_id, event_id=f"referral-premium:{referred_user_id}",
                    title="Un referido activó Premium",
                    message="Una persona invitada mediante tu enlace ha activado Premium.",
                    business_event="referral.premium_activated",
                    metadata={"referral_id": referral_id}, now=current,
                )
            except Exception:
                # Referral attribution and rewards must not fail because notification
                # storage is temporarily unavailable; the user can retry through the
                # idempotent referral reconciliation path.
                import logging
                logging.getLogger("ftrade.referrals").exception("No se pudo encolar aviso de referido")
        rewards_applied = self._process_rewards(referrer_id, current)
        return {"recorded": bool(inserted), "referrer_id": referrer_id, "referral_id": referral_id,
                "duplicate": not inserted, "rewards_applied": rewards_applied}

    def _process_rewards(self, referrer_id: str, now: datetime) -> int:
        applied_count = 0
        # Process prior durable claims first, then claim further batches while enough
        # unused referred accounts exist. If activation fails, leave claim pending.
        for _ in range(20):
            pending = self.repository.list_pending_referral_rewards(referrer_id)
            reward = pending[0] if pending else self.repository.claim_referral_reward(referrer_id, now=now)
            if not reward:
                break
            try:
                self.plan_service.activate_premium(
                    referrer_id,
                    days=REWARD_DAYS,
                    source="referral",
                    reference=str(reward["reward_id"]),
                    idempotency_key=str(reward["reward_id"]),
                    now=now,
                )
                self.repository.finish_referral_reward(str(reward["reward_id"]), applied=True, now=now)
                if self.business_notifications is not None:
                    try:
                        self.business_notifications.enqueue(
                            referrer_id, event_id=f"referral-reward:{reward['reward_id']}",
                            title="Has recibido días Premium",
                            message=f"Has recibido {REWARD_DAYS} días adicionales de Premium por tus referidos.",
                            business_event="referral.reward_applied",
                            metadata={"reward_id": str(reward["reward_id"]), "days": REWARD_DAYS}, now=now,
                        )
                    except Exception:
                        import logging
                        logging.getLogger("ftrade.referrals").exception("No se pudo encolar aviso de recompensa")
                applied_count += 1
            except Exception as exc:  # durable claim remains retryable
                self.repository.finish_referral_reward(str(reward["reward_id"]), applied=False, now=now, error=type(exc).__name__)
                break
        return applied_count

    def retry_pending_rewards(self, user_id: str, *, now: datetime | None = None) -> int:
        return self._process_rewards(str(user_id), now or _now())

    def summary(self, user_id: str) -> dict[str, Any]:
        user = self.auth_repository.get_user_by_id(str(user_id))
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        ref_code = str(user.get("ref_code") or f"ref_{user_id}")
        # Referral links must land on the public web app, not in the Telegram bot.
        # FRONTEND_URL can be customized per deployment; use the production web
        # domain as a safe default for the current Railway deployment.
        default_frontend_url = "https://keryntix-market-desk-production.up.railway.app"
        frontend_url = (
            os.getenv("FRONTEND_URL")
            or os.getenv("PUBLIC_FRONTEND_URL")
            or default_frontend_url
        ).strip().rstrip("/")
        # During the brand/domain migration, Railway may still have the previous
        # FRONTEND_URL saved. Replace only that known legacy host; preserve custom domains.
        try:
            configured_host = (urlsplit(frontend_url).hostname or "").lower()
        except ValueError:
            configured_host = ""
        if configured_host == "ftrade-web-production.up.railway.app":
            frontend_url = default_frontend_url
        link = f"{frontend_url}/r/{quote(ref_code, safe='')}"
        total_referred = self.auth_repository.count_referred_users(str(user_id))
        records = self.repository.list_referrals(str(user_id))
        premium = [r for r in records if str(r.get("activated_plan", "")).lower() == "premium"]
        applied = sum(1 for r in premium if r.get("reward_applied"))
        unclaimed = sum(1 for r in premium if not r.get("reward_applied"))
        list_rewards = getattr(self.repository, "list_referral_rewards", None)
        rewards = list_rewards(str(user_id), limit=100) if callable(list_rewards) else []
        return {
            "ref_code": ref_code,
            "referral_link": link,
            "total_referred": total_referred,
            "free_or_pending_referred": max(0, total_referred - len(records)),
            "premium_referred": len(premium),
            "premium_pending_reward": unclaimed,
            "reward_batches_earned": applied // REWARD_REFERRALS,
            "reward_threshold": REWARD_REFERRALS,
            "reward_days": REWARD_DAYS,
            "rewards": [{"id": str(r.get("reward_id") or ""), "days": int(r.get("duration_days") or REWARD_DAYS),
                         "status": str(r.get("status") or "pending"), "created_at": _iso(r.get("created_at")),
                         "applied_at": _iso(r.get("applied_at"))} for r in rewards],
            "referrals": [{"referred_id": str(r.get("referred_id")), "activated_plan": r.get("activated_plan"),
                           "activated_at": _iso(r.get("activated_at")), "reward_applied": bool(r.get("reward_applied"))}
                          for r in records[-100:]],
        }
