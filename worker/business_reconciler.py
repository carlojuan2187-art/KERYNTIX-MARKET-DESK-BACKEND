"""Periodic recovery of durable billing/referral work.

The reconciler only retries state transitions that have already been persisted.
It never verifies an unconfirmed chain transfer, invents a transaction, or grants
Premium to a merely pending payment.

Run separately: python -m worker.business_reconciler
"""
from __future__ import annotations

from api.logging_config import configure_logging

import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from api.cache import JsonCache, cache_from_environment
from api.repositories.auth import auth_repository_from_environment
from api.repositories.business import business_repository_from_environment
from api.services.payments import PaymentService
from api.services.auth import AuthService
from api.repositories.user_features import user_feature_repository_from_environment
from api.services.business_notifications import BusinessNotificationService
from api.services.event_bus import publish_account_event, publish_payment_updated
from api.services.plans import PlanService
from api.services.referrals import ReferralService
from worker.distributed_lease import DistributedWorkerLease

logger = logging.getLogger("ftrade.business_reconciler")
STATUS_KEY = "business-reconciliation:status:v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BusinessReconciler:
    def __init__(self, business_repository: Any, payment_service: PaymentService,
                 referral_service: ReferralService, cache: JsonCache, *, batch_size: int = 200,
                 auth_repository: Any | None = None) -> None:
        self.repository = business_repository
        self.payment_service = payment_service
        self.referral_service = referral_service
        self.cache = cache
        self.auth_repository = auth_repository
        self.batch_size = max(1, min(int(batch_size), 500))

    async def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or _utc_now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)

        expired_count = await asyncio.to_thread(self.repository.expire_payments, current)
        pending_activations = await asyncio.to_thread(
            self.repository.list_payments_needing_activation, limit=self.batch_size
        )
        activation_completed = 0
        activation_pending = 0
        activation_skipped = 0
        for payment in pending_activations:
            payment_id = str(payment.get("payment_id") or "")
            if not payment_id:
                activation_skipped += 1
                continue
            try:
                result = await asyncio.to_thread(
                    self.payment_service.reconcile_verified_activation, payment_id, now=current
                )
                if result.get("result") == "completed":
                    activation_completed += 1
                    # Publish state changes discovered by background recovery too;
                    # REST remains canonical if a live event cannot be delivered.
                    try:
                        payment_doc = await asyncio.to_thread(self.payment_service.repository.get_payment, payment_id)
                        if payment_doc:
                            target_user = str(payment_doc.get("user_id") or "")
                            public_payment = self.payment_service._public(payment_doc)
                            if target_user:
                                await publish_payment_updated(self.cache, target_user, public_payment)
                                if self.auth_repository is not None:
                                    user_snapshot = await asyncio.to_thread(AuthService(self.auth_repository).me, target_user)
                                    await publish_account_event(self.cache, target_user, "plan.changed", {"user": user_snapshot})
                    except Exception as exc:
                        logger.warning("No se pudo publicar el estado de una activación reconciliada (%s)", type(exc).__name__)
                elif result.get("result") == "activation_pending":
                    activation_pending += 1
                else:
                    activation_skipped += 1
            except Exception:
                activation_pending += 1
                logger.exception("No se pudo reconciliar activación para el pago %s", payment_id)

        referrer_ids = await asyncio.to_thread(
            self.repository.list_pending_referral_reward_referrers, limit=self.batch_size
        )
        referral_rewards_applied = 0
        referral_referrers_pending = 0
        for referrer_id in referrer_ids:
            try:
                applied = await asyncio.to_thread(
                    self.referral_service.retry_pending_rewards, str(referrer_id), now=current
                )
                referral_rewards_applied += int(applied)
                if applied and self.auth_repository is not None:
                    try:
                        user_snapshot = await asyncio.to_thread(AuthService(self.auth_repository).me, str(referrer_id))
                        await publish_account_event(self.cache, str(referrer_id), "plan.changed", {"user": user_snapshot})
                    except Exception as exc:
                        logger.warning("No se pudo publicar actualización de plan por referido (%s)", type(exc).__name__)
                remains = await asyncio.to_thread(
                    self.repository.list_pending_referral_rewards, str(referrer_id)
                )
                if remains:
                    referral_referrers_pending += 1
            except Exception:
                referral_referrers_pending += 1
                logger.exception("No se pudo reconciliar recompensa de referidos para %s", referrer_id)

        result = {
            "status": "degraded" if activation_pending or referral_referrers_pending else "ok",
            "checked_at": current.isoformat().replace("+00:00", "Z"),
            "payments_expired": int(expired_count),
            "verified_activations_seen": len(pending_activations),
            "verified_activations_completed": activation_completed,
            "verified_activations_pending": activation_pending,
            "verified_activations_skipped": activation_skipped,
            "referrers_with_pending_rewards_seen": len(referrer_ids),
            "referral_rewards_applied": referral_rewards_applied,
            "referrers_still_pending": referral_referrers_pending,
            "batch_size": self.batch_size,
        }
        try:
            await self.cache.set_json(STATUS_KEY, result, ttl_seconds=3600)
        except Exception:
            logger.exception("No se pudo publicar el estado del reconciliador de negocio")
        return result


async def _run_worker() -> None:
    configure_logging()
    auth_repository = auth_repository_from_environment()
    auth_health = auth_repository.health()
    if not auth_health.get("persistent") and os.getenv("BUSINESS_RECONCILIATION_ALLOW_MEMORY", "false").lower() not in {"1", "true", "yes"}:
        auth_repository.close()
        raise RuntimeError("El reconciliador requiere usuarios y planes persistentes en MongoDB")
    repository = business_repository_from_environment(auth_repository)
    business_health = repository.health()
    if not business_health.get("persistent") and os.getenv("BUSINESS_RECONCILIATION_ALLOW_MEMORY", "false").lower() not in {"1", "true", "yes"}:
        repository.close()
        auth_repository.close()
        raise RuntimeError("El reconciliador requiere persistencia de pagos y recompensas en MongoDB")
    cache = cache_from_environment()
    user_features = user_feature_repository_from_environment()
    plan_service = PlanService(auth_repository)
    business_notifications = BusinessNotificationService(auth_repository, user_features, cache)
    referral_service = ReferralService(auth_repository, repository, plan_service, business_notifications=business_notifications)
    payment_service = PaymentService(repository, plan_service, referral_service=referral_service,
                                     business_notifications=business_notifications)
    worker = BusinessReconciler(
        repository, payment_service, referral_service, cache,
        batch_size=int(os.getenv("BUSINESS_RECONCILIATION_BATCH_SIZE", "200")),
        auth_repository=auth_repository,
    )
    interval = max(10.0, min(3600.0, float(os.getenv("BUSINESS_RECONCILIATION_INTERVAL_SECONDS", "30"))))
    backoff = max(5.0, min(600.0, float(os.getenv("BUSINESS_RECONCILIATION_ERROR_BACKOFF_SECONDS", "20"))))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass

    try:
        async with DistributedWorkerLease(cache, "business-reconciler"):
            logger.info("Reconciliador de negocio iniciado; intervalo=%ss; storage=%s", interval, business_health.get("adapter"))
            while not stop.is_set():
                began = time.monotonic()
                try:
                    status = await worker.run_once()
                    logger.info("Reconciliación de negocio: %s", status)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Falló el ciclo de reconciliación de negocio")
                    try:
                        await cache.set_json(STATUS_KEY, {
                            "status": "degraded", "error": "BUSINESS_RECONCILIATION_FAILED",
                            "checked_at": _utc_now().isoformat().replace("+00:00", "Z"),
                        }, ttl_seconds=3600)
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    continue
                delay = max(0.0, interval - (time.monotonic() - began))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
    finally:
        await cache.close()
        user_features.close()
        repository.close()
        auth_repository.close()


def main() -> None:
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
