"""Redis-backed singleton lease for long-running FTrade workers.

A worker is considered active only while it owns a renewable lease. Redis uses
compare-and-set scripts so a stale process cannot renew or release another
process's lock. The in-memory adapter is for local tests only.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("ftrade.worker_lease")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class DistributedWorkerLease:
    def __init__(self, cache: Any, worker_name: str, *, ttl_seconds: int | None = None,
                 retry_seconds: float | None = None) -> None:
        self.cache = cache
        self.worker_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(worker_name)).strip("-")[:80]
        if not self.worker_name:
            raise ValueError("worker_name es obligatorio")
        namespace = os.getenv("FTRADE_DEPLOYMENT_NAME", os.getenv("FTRADE_ENV", "development")).strip().lower()
        namespace = "".join(c if c.isalnum() or c in "-_" else "-" for c in namespace).strip("-") or "default"
        self.key = f"ftrade:worker-lease:{namespace}:{self.worker_name}:v1"
        self.status_key = f"ftrade:worker-status:{namespace}:{self.worker_name}:v1"
        self.token = uuid.uuid4().hex
        self.ttl_seconds = max(15, min(3600, int(ttl_seconds or os.getenv("WORKER_LEASE_TTL_SECONDS", "90"))))
        self.retry_seconds = max(1.0, min(30.0, float(retry_seconds or os.getenv("WORKER_LEASE_RETRY_SECONDS", "5"))))
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._owner_task: asyncio.Task[Any] | None = None
        self._closing = False
        self._acquired = False

    async def __aenter__(self) -> "DistributedWorkerLease":
        health = await self.cache.health()
        if os.getenv("FTRADE_ENV", "development").strip().lower() == "production" and not health.get("shared", False):
            raise RuntimeError("Los workers de producción requieren coordinación Redis compartida")
        self._owner_task = asyncio.current_task()
        while not self._acquired:
            self._acquired = await self.cache.acquire_lease(self.key, self.token, ttl_seconds=self.ttl_seconds)
            if not self._acquired:
                logger.warning("Worker %s en espera: ya existe una instancia con lease activo", self.worker_name)
                await asyncio.sleep(self.retry_seconds)
        await self._write_status("running")
        self._heartbeat_task = asyncio.create_task(self._heartbeat(), name=f"lease-heartbeat:{self.worker_name}")
        logger.info("Lease adquirida para worker %s", self.worker_name)
        return self

    async def _write_status(self, status: str, *, error: str | None = None) -> None:
        doc = {
            "worker": self.worker_name,
            "status": status,
            "host": platform.node()[:120],
            "pid": os.getpid(),
            "updated_at": _utc_now(),
        }
        if error:
            doc["error"] = error[:120]
        try:
            await self.cache.set_json(self.status_key, doc, ttl_seconds=max(60, self.ttl_seconds * 3))
        except Exception:
            logger.warning("No se pudo publicar el estado del worker %s", self.worker_name, exc_info=True)

    async def _heartbeat(self) -> None:
        interval = max(3.0, self.ttl_seconds / 3.0)
        try:
            while True:
                await asyncio.sleep(interval)
                renewed = await self.cache.renew_lease(self.key, self.token, ttl_seconds=self.ttl_seconds)
                if not renewed:
                    await self._write_status("lease_lost", error="LEASE_RENEWAL_FAILED")
                    logger.critical("Se perdió el lease distribuido del worker %s; se cancela para evitar procesamiento duplicado", self.worker_name)
                    if not self._closing and self._owner_task and not self._owner_task.done():
                        self._owner_task.cancel()
                    return
                await self._write_status("running")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Fail closed: without confirmed lease renewal, continuing the worker can
            # create duplicate scans, alerts, or recurring business operations.
            await self._write_status("lease_lost", error=type(exc).__name__)
            logger.critical("No se pudo renovar el lease de %s", self.worker_name, exc_info=True)
            if not self._closing and self._owner_task and not self._owner_task.done():
                self._owner_task.cancel()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._closing = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._acquired:
            try:
                await self.cache.release_lease(self.key, self.token)
            except Exception:
                logger.error("No se pudo liberar lease del worker %s; expirará por TTL", self.worker_name, exc_info=True)
        await self._write_status("stopped" if exc_type is None else "failed", error=type(exc).__name__ if exc else None)
        self._acquired = False
