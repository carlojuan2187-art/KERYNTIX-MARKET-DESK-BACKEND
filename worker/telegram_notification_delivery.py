"""Durable, retryable Telegram delivery for persisted user notifications.

Run as its own service: ``python -m worker.telegram_notification_delivery``.
The inbox notification and this outbox payload are stored in the same MongoDB
record, so queueing cannot be lost between two database writes. Delivery is
at-least-once: Telegram does not provide an idempotency key, so a process crash
between Telegram accepting a message and this worker recording success can
rarely lead to a repeated message.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from api.cache import JsonCache, cache_from_environment
from api.repositories.auth import AuthRepository, auth_repository_from_environment
from api.repositories.user_features import UserFeatureRepository, user_feature_repository_from_environment
from api.services.auth import is_admin_user
from api.services.telegram import TelegramBotAPI
from api.services.user_features import alert_preferences, wants_alert
from domain.plans import evaluate_access
from worker.distributed_lease import DistributedWorkerLease

logger = logging.getLogger("ftrade.telegram_notification_delivery")
STATUS_KEY = "telegram-notification-delivery:status:v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


class TelegramNotificationDeliveryWorker:
    def __init__(self, features: UserFeatureRepository, auth: AuthRepository, telegram: Any,
                 cache: JsonCache | None = None, *, clock=_utc_now, batch_size: int = 20,
                 max_attempts: int = 8, lease_seconds: int = 90,
                 retry_base_seconds: int = 5, retry_max_seconds: int = 3600) -> None:
        self.features = features
        self.auth = auth
        self.telegram = telegram
        self.cache = cache
        self.clock = clock
        self.batch_size = max(1, min(int(batch_size), 100))
        self.max_attempts = max(1, min(int(max_attempts), 25))
        self.lease_seconds = max(15, min(int(lease_seconds), 600))
        self.retry_base_seconds = max(1, min(int(retry_base_seconds), 300))
        self.retry_max_seconds = max(self.retry_base_seconds, min(int(retry_max_seconds), 86400))

    async def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or self.clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)

        is_configured = getattr(self.telegram, "is_configured", None)
        if callable(is_configured) and not is_configured():
            result = {
                "status": "not_configured", "checked_at": current.isoformat().replace("+00:00", "Z"),
                "claimed": 0, "sent": 0, "retried": 0, "dead": 0, "suppressed": 0,
            }
            await self._write_status(result)
            return result

        counts: dict[str, Any] = {
            "status": "ok", "checked_at": current.isoformat().replace("+00:00", "Z"),
            "claimed": 0, "sent": 0, "retried": 0, "dead": 0, "suppressed": 0,
        }
        for _ in range(self.batch_size):
            claim_id = uuid.uuid4().hex
            row = await asyncio.to_thread(
                self.features.claim_telegram_delivery,
                now=current,
                lease_until=current + timedelta(seconds=self.lease_seconds),
                claim_id=claim_id,
            )
            if not row:
                break
            counts["claimed"] += 1
            notification_id = str(row.get("id") or "")
            delivery = row.get("telegram_delivery") if isinstance(row.get("telegram_delivery"), Mapping) else {}
            user_id = str(row.get("user_id") or "")

            suppression_reason = await self._suppression_reason(user_id, delivery, row.get("kind"), current, metadata=row.get("metadata"))
            if suppression_reason:
                finished = await asyncio.to_thread(
                    self.features.finish_telegram_delivery, notification_id, claim_id,
                    outcome="suppressed", now=current, error_code=suppression_reason,
                )
                if finished:
                    counts["suppressed"] += 1
                continue

            chat_id = str(delivery.get("chat_id") or "")
            message = str(delivery.get("text") or "")
            if not notification_id or not chat_id or not message:
                await asyncio.to_thread(
                    self.features.finish_telegram_delivery, notification_id, claim_id,
                    outcome="dead", now=current, error_code="INVALID_OUTBOX_PAYLOAD",
                )
                counts["dead"] += 1
                continue

            try:
                await asyncio.to_thread(self.telegram.send_message, chat_id, message)
            except Exception as exc:
                attempts = int((delivery.get("attempts") or 1))
                retryable = bool(getattr(exc, "retryable", True))
                retry_after = getattr(exc, "retry_after_seconds", None)
                if retryable and attempts < self.max_attempts:
                    exponential = min(self.retry_max_seconds, self.retry_base_seconds * (2 ** max(0, attempts - 1)))
                    delay = max(exponential, int(retry_after or 0))
                    delay = min(self.retry_max_seconds, max(1, delay))
                    next_attempt = current + timedelta(seconds=delay)
                    finished = await asyncio.to_thread(
                        self.features.finish_telegram_delivery, notification_id, claim_id,
                        outcome="retry", now=current, next_attempt_at=next_attempt,
                        error_code=type(exc).__name__,
                    )
                    counts["retried"] += int(bool(finished))
                    if not finished:
                        logger.warning("No se pudo registrar reintento de Telegram para %s: lease perdida", notification_id)
                else:
                    finished = await asyncio.to_thread(
                        self.features.finish_telegram_delivery, notification_id, claim_id,
                        outcome="dead", now=current, error_code=type(exc).__name__,
                    )
                    counts["dead"] += int(bool(finished))
                    if not retryable:
                        logger.info("Entrega de Telegram terminal para notificación %s (%s)", notification_id, type(exc).__name__)
                    else:
                        logger.warning("Entrega de Telegram agotó reintentos para notificación %s", notification_id)
            else:
                finished = await asyncio.to_thread(
                    self.features.finish_telegram_delivery, notification_id, claim_id,
                    outcome="sent", now=current,
                )
                counts["sent"] += int(bool(finished))
                if not finished:
                    # Telegram accepted the request, but the claim was lost before
                    # persistence; a lease recovery may resend (at-least-once semantics).
                    logger.error("Telegram aceptó %s pero no se pudo registrar el éxito; puede repetirse tras expirar el lease", notification_id)

        if counts["retried"] or counts["dead"]:
            counts["status"] = "degraded"
        await self._write_status(counts)
        return counts

    async def _suppression_reason(self, user_id: str, delivery: Mapping[str, Any], kind: Any,
                                  now: datetime, *, metadata: Any = None) -> str | None:
        if not user_id:
            return "USER_ID_MISSING"
        user = await asyncio.to_thread(self.auth.get_user_by_id, user_id)
        if not user:
            return "USER_NOT_FOUND"
        expected_chat = str(delivery.get("chat_id") or "")
        current_chat = str(user.get("telegram_chat_id") or "")
        if not expected_chat or expected_chat != current_chat:
            return "TELEGRAM_LINK_CHANGED"
        prefs = alert_preferences(user)
        if not prefs.get("on") or not prefs.get("telegram") or not wants_alert(user, str(kind or "")):
            return "TELEGRAM_NOTIFICATIONS_DISABLED"
        access = evaluate_access(user, now=now, is_admin=is_admin_user(user))
        business_metadata = metadata if isinstance(metadata, Mapping) else {}
        transactional = str(kind or "").lower() == "plans" and business_metadata.get("transactional") is True
        if transactional:
            # Billing/plan status should still reach an expired account; bans remain
            # an explicit suppression condition. Market alerts retain normal gating.
            if access.get("ban_active"):
                return "ACCOUNT_BANNED"
        elif not access.get("allowed"):
            return "ACCOUNT_NOT_ALLOWED"
        return None

    async def _write_status(self, result: dict[str, Any]) -> None:
        if not self.cache:
            return
        try:
            await self.cache.set_json(STATUS_KEY, result, ttl_seconds=3600)
        except Exception:
            logger.warning("No se pudo guardar el estado del worker de entrega Telegram", exc_info=True)


async def _run_worker() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    auth = auth_repository_from_environment()
    features = user_feature_repository_from_environment()
    cache = cache_from_environment()
    auth_health = auth.health()
    feature_health = features.health()
    production = os.getenv("FTRADE_ENV", "development").strip().lower() == "production"
    if production and (not auth_health.get("persistent") or not feature_health.get("persistent")):
        features.close()
        auth.close()
        await cache.close()
        raise RuntimeError("El worker de entrega Telegram requiere MongoDB persistente en producción")

    telegram = TelegramBotAPI()
    worker = TelegramNotificationDeliveryWorker(
        features, auth, telegram, cache,
        batch_size=_env_int("TELEGRAM_DELIVERY_BATCH_SIZE", 20, low=1, high=100),
        max_attempts=_env_int("TELEGRAM_DELIVERY_MAX_ATTEMPTS", 8, low=1, high=25),
        lease_seconds=_env_int("TELEGRAM_DELIVERY_LEASE_SECONDS", 90, low=15, high=600),
        retry_base_seconds=_env_int("TELEGRAM_DELIVERY_RETRY_BASE_SECONDS", 5, low=1, high=300),
        retry_max_seconds=_env_int("TELEGRAM_DELIVERY_RETRY_MAX_SECONDS", 3600, low=30, high=86400),
    )
    interval = max(1.0, min(60.0, float(os.getenv("TELEGRAM_DELIVERY_INTERVAL_SECONDS", "3"))))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass
    try:
        async with DistributedWorkerLease(cache, "telegram-notification-delivery"):
            logger.info("Worker de entrega Telegram iniciado; intervalo=%ss; batch=%s", interval, worker.batch_size)
            while not stop.is_set():
                started = time.monotonic()
                try:
                    status = await worker.run_once()
                    if status.get("claimed") or status.get("status") != "ok":
                        logger.info("Entrega Telegram: %s", status)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Falló el ciclo de entrega de notificaciones Telegram")
                delay = max(0.1, interval - (time.monotonic() - started))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
    finally:
        features.close()
        auth.close()
        await cache.close()


def main() -> None:
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
