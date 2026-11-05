"""Read-only HTTP endpoints for KERYNTIX MARKET DESK signals."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from api.repositories.signals import RepositoryUnavailable
from api.schemas import SignalApiResponse
from domain.signals import CanonicalSignalResult, OutcomeState
from api.security import Principal, require_analytical_access
from api.services.signals import SignalDataIntegrityError, SignalService
from api.services.stats_baseline import get_reset_at, rows_since

router = APIRouter(prefix="/signals", tags=["signals"])


def _service(request: Request) -> SignalService:
    return SignalService(
        request.app.state.signal_repository,
        cancelled_display_minutes=request.app.state.cancelled_display_minutes,
    )


def _filter_expired_snapshot(items: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    visible = []
    for item in items:
        if str(item.get("status") or "").upper() == "PENDING":
            expires = item.get("expires_at") or item.get("entry_expires_at")
            if isinstance(expires, str):
                try:
                    expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                except ValueError:
                    expires = None
            if isinstance(expires, datetime):
                expires = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires.astimezone(timezone.utc)
                if expires <= now:
                    continue
        visible.append(item)
    return visible


async def _refresh_visible_snapshot(request: Request, key: str, limit: int) -> list[dict]:
    state = request.app.state
    lock = getattr(state, "signal_snapshot_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        state.signal_snapshot_lock = lock
    async with lock:
        cache = state.market_cache
        try:
            current = await cache.get_json(key)
            if isinstance(current, dict):
                age = time.time() - float(current.get("updated_epoch", 0) or 0)
                if 0 <= age <= 1.0 and isinstance(current.get("items"), list):
                    return _filter_expired_snapshot(current["items"])
        except Exception:
            pass
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(_service(request).list_visible, limit=limit),
                timeout=4.0,
            )
        except asyncio.TimeoutError as exc:
            raise RepositoryUnavailable("La consulta de señales superó el límite de 4 segundos") from exc
        payload = [row.model_dump(mode="json") for row in rows]
        try:
            await cache.set_json(key, {"items": payload, "updated_epoch": time.time()}, ttl_seconds=120)
        except Exception:
            # A Redis cache write must never block a valid response from MongoDB.
            pass
        return payload


def _schedule_visible_refresh(request: Request, key: str, limit: int) -> asyncio.Task:
    state = request.app.state
    tasks = getattr(state, "signal_snapshot_tasks", None)
    if tasks is None:
        tasks = {}
        state.signal_snapshot_tasks = tasks
    task = tasks.get(key)
    if task is not None and not task.done():
        return task
    task = asyncio.create_task(_refresh_visible_snapshot(request, key, limit))
    tasks[key] = task

    def clear(done: asyncio.Task) -> None:
        if tasks.get(key) is done:
            tasks.pop(key, None)
        if not done.cancelled():
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                # The next request may retry; do not keep a failed task pinned.
                state = request.app.state
                state.signal_snapshot_last_error = str(error)[:200]

    task.add_done_callback(clear)
    return task


@router.get("", response_model=list[SignalApiResponse])
async def list_signals(
    request: Request,
    limit: int = Query(default=10, ge=1, le=10),
    _principal: Principal = Depends(require_analytical_access),
) -> list[dict]:
    """Return a cached signal snapshot immediately and refresh it single-flight."""
    key = f"signals:visible:v2:{limit}"
    cache = request.app.state.market_cache
    cached_items: list[dict] | None = None
    cached_age = float("inf")
    try:
        cached = await cache.get_json(key)
        if isinstance(cached, dict) and isinstance(cached.get("items"), list):
            cached_items = _filter_expired_snapshot(cached["items"])
            cached_age = max(0.0, time.time() - float(cached.get("updated_epoch", 0) or 0))
    except Exception:
        cached_items = None

    if cached_items is not None and cached_age <= 2.0:
        return cached_items

    task = _schedule_visible_refresh(request, key, limit)
    if cached_items is not None and cached_age <= 12.0:
        # Keep the last known valid signals visible while one refresh runs.
        return cached_items

    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=4.25)
    except asyncio.TimeoutError as exc:
        if cached_items is not None and cached_age <= 12.0:
            return cached_items
        raise HTTPException(status_code=503, detail={"code": "SIGNALS_WARMING", "message": "Las señales se están actualizando. Vuelve a intentarlo en unos segundos."}) from exc
    except RepositoryUnavailable as exc:
        if cached_items is not None and cached_age <= 12.0:
            return cached_items
        raise HTTPException(status_code=503, detail={"code": "DATA_UNAVAILABLE", "message": "Los datos de señales no están disponibles temporalmente."}) from exc
    except SignalDataIntegrityError as exc:
        raise HTTPException(status_code=503, detail={"code": "SIGNAL_DATA_INVALID", "message": "Hay una señal almacenada que no cumple el contrato de datos."}) from exc


@router.get("/results", response_model=list[CanonicalSignalResult])
def list_signal_results(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    _principal: Principal = Depends(require_analytical_access),
) -> list[CanonicalSignalResult]:
    """Return immutable closed-trade snapshots (TP1/TP2/SL), newest first."""
    try:
        rows = request.app.state.signal_repository.list_results(limit=limit)
        # Admin statistics reset: count only trades closed after the cut-off.
        rows = rows_since(rows, get_reset_at(request.app.state.business_repository))
    except RepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "DATA_UNAVAILABLE", "message": "Los resultados no están disponibles temporalmente."}) from exc
    output: list[CanonicalSignalResult] = []
    for row in rows:
        data = dict(row)
        data.pop("_id", None)
        data.setdefault("id", str(data.get("signal_id") or ""))
        data.setdefault("schema_version", "1.0")
        try:
            output.append(CanonicalSignalResult.model_validate(data))
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "RESULT_DATA_INVALID", "message": "Hay un resultado almacenado que no cumple el contrato de datos."}) from exc
    return output


@router.get("/{symbol}", response_model=SignalApiResponse)
def get_signal_by_symbol(
    symbol: str,
    request: Request,
    _principal: Principal = Depends(require_analytical_access),
) -> SignalApiResponse:
    normalized = symbol.strip().upper()
    if not normalized or len(normalized) > 40:
        raise HTTPException(status_code=422, detail={"code": "INVALID_SYMBOL", "message": "Símbolo inválido."})
    try:
        signal = _service(request).latest_for_symbol(normalized)
    except RepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "DATA_UNAVAILABLE", "message": "Los datos de señales no están disponibles temporalmente."}) from exc
    except SignalDataIntegrityError as exc:
        raise HTTPException(status_code=503, detail={"code": "SIGNAL_DATA_INVALID", "message": "La señal almacenada no cumple el contrato de datos."}) from exc
    if signal is None:
        raise HTTPException(status_code=404, detail={"code": "SIGNAL_NOT_FOUND", "message": f"No se encontró una señal para {normalized}."})
    return signal
