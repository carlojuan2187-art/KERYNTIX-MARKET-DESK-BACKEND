"""Durable Web Push delivery worker for authenticated browser subscriptions.

The notification and its push outbox live in the same MongoDB document. Delivery
is at-least-once: the push service may accept a request immediately before a worker
crash, so browsers should also deduplicate via the notification `tag`/id.
"""
from __future__ import annotations

from api.logging_config import configure_logging

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
from api.services.auth import is_admin_id
from api.services.user_features import alert_preferences, wants_alert
from api.services.web_push import PyWebPushGateway, WebPushSendError
from domain.plans import evaluate_access
from worker.distributed_lease import DistributedWorkerLease

logger = logging.getLogger("ftrade.web_push_delivery")
STATUS_KEY = "web-push-delivery:status:v1"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _env_int(name: str, default: int, *, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return None


def _kind_for_preferences(kind: str) -> str:
    key = str(kind or "").lower()
    return key if key in {"liq", "mov", "vol", "signals", "price", "plans"} else "signals"


class WebPushDeliveryWorker:
    def __init__(self, features: UserFeatureRepository, auth: AuthRepository, gateway: Any,
                 cache: JsonCache | None = None, *, clock=_utc_now, batch_size: int = 20,
                 max_attempts: int = 8, lease_seconds: int = 90,
                 retry_base_seconds: int = 5, retry_max_seconds: int = 3600,
                 ttl_seconds: int = 300) -> None:
        self.features = features
        self.auth = auth
        self.gateway = gateway
        self.cache = cache
        self.clock = clock
        self.batch_size = max(1, min(int(batch_size), 100))
        self.max_attempts = max(1, min(int(max_attempts), 25))
        self.lease_seconds = max(15, min(int(lease_seconds), 600))
        self.retry_base_seconds = max(1, min(int(retry_base_seconds), 300))
        self.retry_max_seconds = max(self.retry_base_seconds, min(int(retry_max_seconds), 86400))
        self.ttl_seconds = max(60, min(int(ttl_seconds), 86400))

    async def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or self.clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        configured = getattr(self.gateway, "is_configured", None)
        if callable(configured) and not configured():
            result = {"status": "not_configured", "checked_at": current.isoformat().replace("+00:00", "Z"),
                      "claimed": 0, "sent": 0, "retried": 0, "dead": 0, "suppressed": 0,
                      "endpoints_sent": 0, "endpoints_expired": 0}
            await self._write_status(result)
            return result

        counts: dict[str, Any] = {"status": "ok", "checked_at": current.isoformat().replace("+00:00", "Z"),
                                  "claimed": 0, "sent": 0, "retried": 0, "dead": 0, "suppressed": 0,
                                  "endpoints_sent": 0, "endpoints_expired": 0}
        for _ in range(self.batch_size):
            claim_id = uuid.uuid4().hex
            row = await asyncio.to_thread(
                self.features.claim_web_push_delivery,
                now=current, lease_until=current + timedelta(seconds=self.lease_seconds), claim_id=claim_id,
            )
            if not row:
                break
            counts["claimed"] += 1
            notification_id = str(row.get("id") or "")
            user_id = str(row.get("user_id") or "")
            delivery = row.get("web_push_delivery") if isinstance(row.get("web_push_delivery"), Mapping) else {}
            try:
                user = await asyncio.to_thread(self.auth.get_user_by_id, user_id)
            except Exception:
                user = None
                logger.exception("No se pudo validar el usuario antes de Web Push (%s)", notification_id)
            prefs = alert_preferences(user) if user else {}
            access = evaluate_access(user or {}, now=current, is_admin=is_admin_id(user_id)) if user else {"allowed": False}
            category = _kind_for_preferences(str(row.get("kind") or ""))
            metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
            transactional = str(row.get("kind") or "").lower() == "plans" and metadata.get("transactional") is True
            account_blocked = bool(access.get("ban_active")) if transactional else not bool(access.get("allowed"))
            if (not user or account_blocked or not prefs.get("on", True)
                    or not prefs.get("web_push", False) or not prefs.get(category, True)):
                await self._finish(notification_id, claim_id, "suppressed", current, code="PUSH_PREFERENCE_OR_ACCESS_DISABLED")
                counts["suppressed"] += 1
                continue

            try:
                subscriptions = await asyncio.to_thread(self.features.list_push_subscriptions, user_id, limit=10)
            except Exception:
                await self._finish(notification_id, claim_id, "retry", current + timedelta(seconds=self._backoff(int(delivery.get("attempts") or 1))),
                                   code="PUSH_SUBSCRIPTIONS_STORE_UNAVAILABLE")
                counts["retried"] += 1
                logger.exception("No se pudieron leer las suscripciones Web Push (%s)", notification_id)
                continue
            if not subscriptions:
                await self._finish(notification_id, claim_id, "suppressed", current, code="NO_ACTIVE_PUSH_SUBSCRIPTIONS")
                counts["suppressed"] += 1
                continue

            payload = self._payload(row)
            endpoint_results = delivery.get("endpoint_results") if isinstance(delivery.get("endpoint_results"), Mapping) else {}
            working_results = {str(k): dict(v) for k, v in endpoint_results.items() if isinstance(v, Mapping)}
            due_retry_at: list[datetime] = []
            any_retry = False
            any_sent = False
            any_dead = False
            lost_claim = False
            for subscription in subscriptions:
                subscription_id = str(subscription.get("id") or "")
                if not subscription_id:
                    continue
                previous = working_results.get(subscription_id, {})
                previous_status = str(previous.get("status") or "pending")
                if previous_status in {"sent", "expired", "dead"}:
                    if previous_status == "sent":
                        any_sent = True
                    elif previous_status == "dead":
                        any_dead = True
                    continue
                next_attempt = _as_utc(previous.get("next_attempt_at"))
                if previous_status == "retry" and next_attempt and next_attempt > current:
                    due_retry_at.append(next_attempt)
                    any_retry = True
                    continue
                endpoint_attempts = int(previous.get("attempts") or 0) + 1
                try:
                    await self.gateway.send(subscription, payload, ttl_seconds=self.ttl_seconds)
                    endpoint_result = {"status": "sent", "attempts": endpoint_attempts, "sent_at": current,
                                       "last_error_code": None}
                    recorded = await asyncio.to_thread(
                        self.features.record_web_push_endpoint_result, notification_id, claim_id, subscription_id,
                        endpoint_result, now=current,
                    )
                    if not recorded:
                        lost_claim = True
                        break
                    working_results[subscription_id] = endpoint_result
                    any_sent = True
                    counts["endpoints_sent"] += 1
                except Exception as exc:
                    code, retryable, expired, retry_after_seconds = self._classify_error(exc)
                    if expired:
                        try:
                            await asyncio.to_thread(self.features.delete_push_subscription_by_id, subscription_id)
                        except Exception:
                            logger.warning("No se pudo eliminar una suscripción push vencida", exc_info=True)
                        endpoint_result = {"status": "expired", "attempts": endpoint_attempts,
                                           "last_error_code": code, "finished_at": current}
                        counts["endpoints_expired"] += 1
                    elif retryable and endpoint_attempts < self.max_attempts:
                        retry_delay = max(self._backoff(endpoint_attempts), retry_after_seconds or 0)
                        retry_at = current + timedelta(seconds=min(self.retry_max_seconds, retry_delay))
                        endpoint_result = {"status": "retry", "attempts": endpoint_attempts,
                                           "next_attempt_at": retry_at, "last_error_code": code}
                        due_retry_at.append(retry_at)
                        any_retry = True
                    else:
                        endpoint_result = {"status": "dead", "attempts": endpoint_attempts,
                                           "last_error_code": code, "finished_at": current}
                        any_dead = True
                    recorded = await asyncio.to_thread(
                        self.features.record_web_push_endpoint_result, notification_id, claim_id, subscription_id,
                        endpoint_result, now=current,
                    )
                    if not recorded:
                        lost_claim = True
                        break
                    working_results[subscription_id] = endpoint_result
            if lost_claim:
                logger.warning("Se perdió la lease de entrega Web Push (%s); el nuevo propietario recuperará la tarea", notification_id)
                continue

            # Recompute pending endpoints from current active subscriptions; this avoids
            # resending to a browser that already accepted the push on a later retry.
            remaining_retry = []
            for subscription in subscriptions:
                sid = str(subscription.get("id") or "")
                result = working_results.get(sid, {})
                if result.get("status") == "retry":
                    retry_at = _as_utc(result.get("next_attempt_at")) or current
                    remaining_retry.append(retry_at)
            if remaining_retry:
                if int(delivery.get("attempts") or 1) >= self.max_attempts:
                    await self._finish(notification_id, claim_id, "dead", current, code="PUSH_RETRY_LIMIT_REACHED")
                    counts["dead"] += 1
                else:
                    next_time = min(remaining_retry)
                    await self._finish(notification_id, claim_id, "retry", next_time, code="PUSH_TEMPORARY_FAILURE")
                    counts["retried"] += 1
            elif any_sent:
                await self._finish(notification_id, claim_id, "sent", current)
                counts["sent"] += 1
            elif any_dead:
                await self._finish(notification_id, claim_id, "dead", current, code="NO_PUSH_ENDPOINT_ACCEPTED")
                counts["dead"] += 1
            else:
                # All subscriptions expired while delivering; no active endpoint remains.
                await self._finish(notification_id, claim_id, "suppressed", current, code="NO_VALID_PUSH_SUBSCRIPTIONS")
                counts["suppressed"] += 1

        await self._write_status(counts)
        return counts

    @staticmethod
    def _payload(row: Mapping[str, Any]) -> dict[str, Any]:
        notification_id = str(row.get("id") or "")
        event_id = str(row.get("event_id") or notification_id)
        metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        return {
            "id": notification_id,
            "event_id": event_id,
            "title": str(row.get("title") or "FTrade")[:120],
            "body": str(row.get("message") or "Tienes una actualización en FTrade.")[:240],
            "icon": "/favicon.svg",
            "badge": "/favicon.svg",
            "tag": f"ftrade:{event_id}"[:120],
            "url": "/",
            "kind": str(row.get("kind") or "general")[:40],
            "symbol": str(row.get("symbol") or "")[:32] or None,
            "metadata": dict(metadata),
            "created_at": str(row.get("created_at") or ""),
        }

    def _backoff(self, attempts: int) -> int:
        return min(self.retry_max_seconds, self.retry_base_seconds * (2 ** min(max(0, attempts - 1), 10)))

    @staticmethod
    def _classify_error(exc: Exception) -> tuple[str, bool, bool, int | None]:
        if isinstance(exc, WebPushSendError):
            return exc.code, exc.retryable, exc.expired, exc.retry_after_seconds
        # Unexpected errors may indicate a transient library/transport issue. Keep the
        # public error code generic; never put exception strings/URLs in persisted logs.
        return f"PUSH_UNEXPECTED_{type(exc).__name__}", True, False, None

    async def _finish(self, notification_id: str, claim_id: str, outcome: str, now: datetime,
                      *, next_attempt_at: datetime | None = None, code: str | None = None) -> None:
        success = await asyncio.to_thread(
            self.features.finish_web_push_delivery, notification_id, claim_id,
            outcome=outcome, now=now, next_attempt_at=next_attempt_at, error_code=code,
        )
        if not success:
            logger.warning("No se pudo finalizar la entrega Web Push %s (lease perdida)", notification_id)

    async def _write_status(self, result: Mapping[str, Any]) -> None:
        if self.cache is None:
            return
        try:
            await self.cache.set_json(STATUS_KEY, dict(result), ttl_seconds=3600)
        except Exception:
            logger.warning("No se pudo registrar el estado del worker Web Push", exc_info=True)


async def _run_forever(cache: JsonCache) -> None:
    from api.services.web_push import PyWebPushGateway
    features = user_feature_repository_from_environment()
    auth = auth_repository_from_environment()
    gateway = PyWebPushGateway()
    worker = WebPushDeliveryWorker(
        features, auth, gateway, cache,
        batch_size=_env_int("WEB_PUSH_BATCH_SIZE", 20, low=1, high=100),
        max_attempts=_env_int("WEB_PUSH_MAX_ATTEMPTS", 8, low=1, high=25),
        lease_seconds=_env_int("WORKER_LEASE_TTL_SECONDS", 90, low=15, high=600),
        retry_base_seconds=_env_int("WEB_PUSH_RETRY_BASE_SECONDS", 5, low=1, high=300),
        retry_max_seconds=_env_int("WEB_PUSH_RETRY_MAX_SECONDS", 3600, low=5, high=86400),
        ttl_seconds=_env_int("WEB_PUSH_TTL_SECONDS", 300, low=60, high=86400),
    )
    try:
        while True:
            await worker.run_once()
            await asyncio.sleep(max(1.0, min(60.0, float(os.getenv("WEB_PUSH_POLL_SECONDS", "3")))))
    finally:
        close_gateway = getattr(gateway, "close", None)
        if callable(close_gateway):
            close_gateway()
        features.close()
        auth.close()


def main() -> None:
    configure_logging()
    stop = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover
            pass

    async def run() -> None:
        cache = cache_from_environment()
        try:
            async with DistributedWorkerLease(cache, "web-push-delivery"):
                task = asyncio.create_task(_run_forever(cache))
                await stop.wait()
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            await cache.close()

    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
