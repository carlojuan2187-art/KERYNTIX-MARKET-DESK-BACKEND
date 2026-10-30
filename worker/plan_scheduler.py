"""Standalone worker for scheduled trial/subscription reminders.

Expiration itself is checked synchronously by the canonical entitlement rule on
all protected API requests. This worker sends one-time messages before and just
after expiry; it never grants access and cannot extend a user's plan.

Run: python -m worker.plan_scheduler
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

from api.cache import cache_from_environment
from api.repositories.auth import auth_repository_from_environment
from api.repositories.user_features import user_feature_repository_from_environment
from api.services.business_notifications import BusinessNotificationService
from api.services.plans import PlanMaintenanceService
from api.services.telegram import TelegramBotAPI

logger = logging.getLogger("ftrade.plan_scheduler")


async def _run() -> None:
    configure_logging()
    repository = auth_repository_from_environment()
    storage = repository.health()
    if not storage.get("persistent") and os.getenv("PLAN_MAINTENANCE_ALLOW_MEMORY", "false").lower() not in {"1", "true", "yes"}:
        raise RuntimeError("El scheduler de planes requiere MongoDB persistente fuera de pruebas/local")
    cache = cache_from_environment()
    features = user_feature_repository_from_environment()
    telegram = TelegramBotAPI()
    business_notifications = BusinessNotificationService(repository, features, cache)
    service = PlanMaintenanceService(repository, telegram, business_notifications=business_notifications)
    interval = max(30, min(3600, int(os.getenv("PLAN_MAINTENANCE_INTERVAL_SECONDS", "60"))))
    backoff = max(10, min(600, int(os.getenv("PLAN_MAINTENANCE_ERROR_BACKOFF_SECONDS", "30"))))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass
    logger.info("Plan scheduler iniciado; intervalo=%ss; storage=%s", interval, storage.get("adapter"))
    try:
        from worker.distributed_lease import DistributedWorkerLease
        async with DistributedWorkerLease(cache, "plan-scheduler"):
            while not stop.is_set():
                began = time.monotonic()
                try:
                    status: dict[str, Any] = service.run_once(now=datetime.now(timezone.utc))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Falló el ciclo de mantenimiento de planes")
                    status = {"status": "degraded", "error": "PLAN_MAINTENANCE_FAILED", "checked_at": datetime.now(timezone.utc).isoformat()}
                    try:
                        await cache.set_json("plan-maintenance:status:v1", status, ttl_seconds=3600)
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=backoff)
                    except asyncio.TimeoutError:
                        pass
                    continue
                try:
                    await cache.set_json("plan-maintenance:status:v1", status, ttl_seconds=3600)
                except Exception as exc:
                    logger.warning("No se pudo publicar estado del scheduler (%s)", type(exc).__name__)
                logger.info("Mantenimiento de planes: %s", status)
                delay = max(0.0, interval - (time.monotonic() - began))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
    finally:
        try:
            await cache.close()
        finally:
            features.close()
            repository.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
