"""Web Push subscription management and safe browser push delivery adapter."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from fastapi import HTTPException

from api.repositories.auth import AuthRepository
from api.repositories.user_features import UserFeatureRepository
from api.web_push_schemas import validate_push_endpoint

logger = logging.getLogger("ftrade.web_push")
UTC = timezone.utc


class WebPushSendError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool, expired: bool = False,
                 retry_after_seconds: int | None = None) -> None:
        super().__init__(code)
        self.code = str(code)[:100]
        self.retryable = bool(retryable)
        self.expired = bool(expired)
        self.retry_after_seconds = max(0, min(int(retry_after_seconds), 86400)) if retry_after_seconds is not None else None


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return moment.isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        return value
    return None


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class PyWebPushGateway:
    """Thin wrapper around pywebpush. The private PEM is never returned to API clients."""

    def __init__(self, *, public_key: str | None = None, private_key_path: str | None = None,
                 private_key_pem: str | None = None, subject: str | None = None) -> None:
        self.public_key = (public_key if public_key is not None else os.getenv("VAPID_PUBLIC_KEY", "")).strip()
        self.subject = (subject if subject is not None else os.getenv("VAPID_SUBJECT", "")).strip()
        self.private_key_path = (private_key_path if private_key_path is not None else os.getenv("VAPID_PRIVATE_KEY_PATH", "")).strip()
        self._private_key_pem = (private_key_pem if private_key_pem is not None else os.getenv("VAPID_PRIVATE_KEY_PEM", "")).strip()
        if not self._private_key_pem:
            encoded_pem = os.getenv("VAPID_PRIVATE_KEY_PEM_B64", "").strip()
            if encoded_pem:
                try:
                    self._private_key_pem = base64.b64decode(encoded_pem, validate=True).decode("utf-8").strip()
                except Exception:
                    self._private_key_pem = ""
        self._temp_private_path: str | None = None

    def _resolve_private_key_path(self) -> str:
        if self.private_key_path and Path(self.private_key_path).is_file():
            return self.private_key_path
        if not self._private_key_pem.startswith(("-----BEGIN PRIVATE KEY-----", "-----BEGIN EC PRIVATE KEY-----")):
            raise WebPushSendError("VAPID_PRIVATE_KEY_MISSING", retryable=False)
        if self._temp_private_path and Path(self._temp_private_path).is_file():
            return self._temp_private_path
        handle = tempfile.NamedTemporaryFile(mode="w", prefix="ftrade-vapid-", suffix=".pem", delete=False, encoding="utf-8")
        try:
            handle.write(self._private_key_pem + "\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
        finally:
            handle.close()
        self._temp_private_path = handle.name
        return handle.name

    def is_configured(self) -> bool:
        private_available = bool(self.private_key_path and Path(self.private_key_path).is_file()) or self._private_key_pem.startswith(("-----BEGIN PRIVATE KEY-----", "-----BEGIN EC PRIVATE KEY-----"))
        return bool(self.public_key and private_available
                    and (self.subject.startswith("mailto:") or self.subject.startswith("https://")))

    def public_key_for_browser(self) -> str:
        if not self.public_key:
            raise HTTPException(status_code=503, detail={"code": "WEB_PUSH_NOT_CONFIGURED", "message": "Las notificaciones push no están configuradas."})
        try:
            raw = _base64url_decode(self.public_key)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "WEB_PUSH_PUBLIC_KEY_INVALID", "message": "La clave pública VAPID no tiene un formato válido."}) from exc
        if len(raw) != 65 or raw[0] != 4:
            raise HTTPException(status_code=503, detail={"code": "WEB_PUSH_PUBLIC_KEY_INVALID", "message": "La clave pública VAPID no tiene un formato válido."})
        return self.public_key

    async def send(self, subscription: Mapping[str, Any], payload: Mapping[str, Any], *, ttl_seconds: int = 300) -> None:
        import asyncio
        await asyncio.to_thread(self._send_sync, subscription, payload, ttl_seconds)

    def _send_sync(self, subscription: Mapping[str, Any], payload: Mapping[str, Any], ttl_seconds: int) -> None:
        try:
            from pywebpush import WebPushException, webpush
        except ImportError as exc:
            raise WebPushSendError("PYWEBPUSH_NOT_INSTALLED", retryable=False) from exc
        try:
            webpush(
                subscription_info={"endpoint": str(subscription["endpoint"]),
                                  "keys": {"p256dh": str(subscription["p256dh"]), "auth": str(subscription["auth"])}},
                data=json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")),
                vapid_private_key=self._resolve_private_key_path(),
                vapid_claims={"sub": self.subject},
                ttl=max(60, min(int(ttl_seconds), 86400)),
            )
        except WebPushException as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            if status in (404, 410):
                raise WebPushSendError(f"PUSH_ENDPOINT_GONE_{status}", retryable=False, expired=True) from exc
            if status == 429 or (isinstance(status, int) and status >= 500) or status is None:
                retry_after = None
                if status == 429 and response is not None:
                    try:
                        retry_after = int(float(response.headers.get("Retry-After", "")))
                    except (TypeError, ValueError, AttributeError):
                        retry_after = None
                raise WebPushSendError(f"PUSH_TRANSIENT_{status or 'NETWORK'}", retryable=True,
                                       retry_after_seconds=retry_after) from exc
            raise WebPushSendError(f"PUSH_REJECTED_{status or 'UNKNOWN'}", retryable=False) from exc
        except (TimeoutError, OSError) as exc:
            raise WebPushSendError("PUSH_NETWORK_ERROR", retryable=True) from exc

    def close(self) -> None:
        if self._temp_private_path:
            try:
                os.unlink(self._temp_private_path)
            except OSError:
                pass
            self._temp_private_path = None


class WebPushService:
    MAX_SUBSCRIPTIONS = 10

    def __init__(self, features: UserFeatureRepository, gateway: Any, *, clock=time.time) -> None:
        self.features = features
        self.gateway = gateway
        self.clock = clock

    def public_key(self) -> dict[str, str]:
        key = self.gateway.public_key_for_browser()
        return {"public_key": key, "application_server_key": key}

    @staticmethod
    def _public(row: Mapping[str, Any]) -> dict[str, Any]:
        endpoint = str(row.get("endpoint") or "")
        parts = urlsplit(endpoint)
        return {"id": str(row.get("id") or ""), "endpoint_origin": f"{parts.scheme}://{parts.netloc}",
                "user_agent": row.get("user_agent"), "created_at": _iso(row.get("created_at")),
                "updated_at": _iso(row.get("updated_at"))}

    def list(self, user_id: str) -> dict[str, Any]:
        rows = self.features.list_push_subscriptions(str(user_id), limit=self.MAX_SUBSCRIPTIONS)
        return {"items": [self._public(row) for row in rows], "total": len(rows), "max_items": self.MAX_SUBSCRIPTIONS}

    def register(self, user_id: str, payload: Mapping[str, Any], *, user_agent: str | None = None) -> dict[str, Any]:
        endpoint = validate_push_endpoint(str(payload.get("endpoint") or ""))
        keys = payload.get("keys") if isinstance(payload.get("keys"), Mapping) else {}
        p256dh, auth = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
        if len(p256dh) < 32 or len(p256dh) > 256 or len(auth) < 8 or len(auth) > 128:
            raise HTTPException(status_code=422, detail={"code": "PUSH_KEYS_INVALID", "message": "Las claves de suscripción no son válidas."})
        if not all(ch.isalnum() or ch in "_-=" for ch in p256dh + auth):
            raise HTTPException(status_code=422, detail={"code": "PUSH_KEYS_INVALID", "message": "Las claves de suscripción no tienen un formato válido."})
        now = _now()
        expiration_time = payload.get("expiration_time")
        if expiration_time is not None and int(expiration_time) <= int(now.timestamp() * 1000):
            raise HTTPException(status_code=422, detail={"code": "PUSH_SUBSCRIPTION_EXPIRED", "message": "El navegador ha enviado una suscripción push vencida."})
        endpoint_hash = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
        existing = self.features.list_push_subscriptions(str(user_id), limit=self.MAX_SUBSCRIPTIONS)
        existing_hashes = {str(row.get("endpoint_hash")) for row in existing}
        if endpoint_hash not in existing_hashes and len(existing) >= self.MAX_SUBSCRIPTIONS:
            raise HTTPException(status_code=409, detail={"code": "PUSH_SUBSCRIPTION_LIMIT", "message": f"Puedes registrar hasta {self.MAX_SUBSCRIPTIONS} navegadores."})
        doc = {"id": uuid.uuid4().hex, "user_id": str(user_id), "endpoint": endpoint,
               "endpoint_hash": endpoint_hash, "p256dh": p256dh, "auth": auth,
               "user_agent": str(user_agent or "")[:300] or None, "expiration_time": payload.get("expiration_time"),
               "active": True, "created_at": now, "updated_at": now}
        try:
            row = self.features.create_or_update_push_subscription(doc)
        except Exception as exc:
            logger.exception("No se pudo persistir la suscripción Web Push")
            raise HTTPException(status_code=503, detail={"code": "PUSH_STORAGE_UNAVAILABLE", "message": "No se pudo guardar la suscripción de notificaciones."}) from exc
        return self._public(row)

    def delete(self, user_id: str, subscription_id: str) -> None:
        if not self.features.delete_push_subscription(str(subscription_id), str(user_id)):
            raise HTTPException(status_code=404, detail={"code": "PUSH_SUBSCRIPTION_NOT_FOUND", "message": "La suscripción no existe."})

    def delete_current(self, user_id: str, endpoint: str) -> bool:
        """Remove only the exact browser endpoint supplied by the authenticated user."""
        safe_endpoint = validate_push_endpoint(endpoint)
        rows = self.features.list_push_subscriptions(str(user_id), limit=self.MAX_SUBSCRIPTIONS)
        for row in rows:
            if str(row.get("endpoint") or "") == safe_endpoint:
                return bool(self.features.delete_push_subscription(str(row.get("id") or ""), str(user_id)))
        return False


def web_push_configuration_issues(gateway: Any) -> list[str]:
    if getattr(gateway, "is_configured", lambda: False)():
        try:
            gateway.public_key_for_browser()
            return []
        except Exception:
            return ["WEB_PUSH_VAPID_PUBLIC_KEY_INVALID"]
    return ["WEB_PUSH_VAPID_NOT_CONFIGURED"]
