"""Authenticated WebSocket event transport with one-use short-lived tickets."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from starlette.websockets import WebSocketState

from api.event_hub import EventHub
from api.security import Principal, current_principal
from domain.plans import evaluate_access
from api.services.auth import is_admin_user, is_admin_id

router = APIRouter(tags=["realtime-events"])
logger = logging.getLogger("ftrade.events")
TICKET_TTL_SECONDS = 60
EVENT_CHANNELS = (
    "signals:new:v1",
    "signals:events:v1",
    "market:events",
    "notifications:events:v1",
    "account:events:v1",
    "market:movers:events:v1",
    "billing:events:v1",
)
# The account row is re-read from Mongo at most this often per connection (and
# immediately after an account/billing/notification event for that user). Access is
# re-evaluated against the cached row on EVERY event, so trial/plan expiry is exact.
ACCESS_RECHECK_SECONDS = 15.0
# One liquidation summary frame per connection every few seconds is enough: the
# summary covers a 15-minute window and the page also refreshes by REST.
LIQUIDATION_FRAME_MIN_INTERVAL = 3.0
SIGNAL_MEMO_SECONDS = 5.0
_signal_memo: dict[str, tuple[float, asyncio.AbstractEventLoop, asyncio.Future]] = {}


def _ticket_key(ticket: str) -> str:
    return "ws:ticket:v1:" + hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def _is_admin(user_id: str, user: Mapping[str, Any] | None = None) -> bool:
    return is_admin_user(user) if user is not None else is_admin_id(str(user_id))


def _session_is_active(auth_repo: Any, session_id: str | None, user_id: str) -> bool:
    if not session_id:
        return os.getenv("FTRADE_ENV", "development").strip().lower() != "production"
    return bool(auth_repo.is_session_active(session_id, user_id, now=datetime.now(timezone.utc)))


async def _try_send_websocket_json(websocket: WebSocket, payload: dict[str, Any]) -> bool:
    """Send one frame, returning False when the peer/transport is already closed.

    Starlette/Uvicorn can raise RuntimeError or OSError when a send races with a
    disconnect. These are normal connection-lifecycle outcomes, not event-transform
    failures; treating them as such caused repeated sends and log floods.
    """
    if (
        websocket.client_state is not WebSocketState.CONNECTED
        or websocket.application_state is not WebSocketState.CONNECTED
    ):
        return False
    try:
        await websocket.send_json(payload)
    except WebSocketDisconnect:
        return False
    except (RuntimeError, OSError) as exc:
        logger.debug("Transporte WebSocket cerrado durante el envío (%s)", type(exc).__name__)
        return False
    return True


def _row_id(row: Mapping[str, Any]) -> str:
    return str(row.get("id") or row.get("signal_id") or row.get("_id") or "")


def _json_signal(row: Mapping[str, Any]) -> dict[str, Any]:
    copy = dict(row)
    oid = copy.pop("_id", None)
    if not copy.get("id") and oid is not None:
        copy["id"] = str(oid)
    result = jsonable_encoder(copy)
    return result if isinstance(result, dict) else {}


async def _find_signal(request: Request, signal_id: str) -> dict[str, Any] | None:
    try:
        rows = await asyncio.to_thread(request.app.state.signal_repository.list_signals, limit=2000)
    except Exception:
        logger.exception("No se pudo cargar la señal para un evento WebSocket")
        return None
    for row in rows:
        if _row_id(row) == str(signal_id):
            return _json_signal(row)
    return None


async def _load_signal_shared(request: Request, signal_id: str, event_key: Any) -> dict[str, Any] | None:
    """Resolve a signal once per distinct event and share it between all clients.

    Every connected browser used to run its own `list_signals(limit=2000)` query for
    the same event. The first connection performs the lookup; the rest await the same
    future. Each caller gets a shallow copy because frames add top-level keys.
    """
    key = f"{signal_id}|{json.dumps(event_key, sort_keys=True, default=str)}"
    now = time.monotonic()
    loop = asyncio.get_running_loop()
    for stale in [k for k, (stamp, _l, _f) in _signal_memo.items() if now - stamp > SIGNAL_MEMO_SECONDS]:
        _signal_memo.pop(stale, None)
    entry = _signal_memo.get(key)
    if entry is not None and entry[1] is loop:
        result = await entry[2]
        return dict(result) if isinstance(result, dict) else None
    future: asyncio.Future = loop.create_future()
    _signal_memo[key] = (now, loop, future)
    result: dict[str, Any] | None = None
    try:
        result = await _find_signal(request, signal_id)
    finally:
        if result is None:
            # Do not memoize failures/misses: the next event retries the lookup.
            _signal_memo.pop(key, None)
        # Always resolve the future so waiting clients never hang (None on failure).
        future.set_result(result)
    return dict(result) if isinstance(result, dict) else None


@router.post("/events/ticket")
async def create_websocket_ticket(request: Request, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Exchange a normal bearer-authenticated request for a 60-second single-use WS ticket.

    WebSocket browser APIs cannot set an Authorization header. The short-lived ticket
    avoids putting the reusable access token in a URL and is atomically consumed by Redis.
    """
    session_id = principal.claims.get("sid")
    if os.getenv("FTRADE_ENV", "development").strip().lower() == "production" and not session_id:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "La sesión no permite abrir el canal en tiempo real."})
    ticket = secrets.token_urlsafe(32)
    payload = {
        "user_id": str(principal.user_id),
        "session_id": str(session_id) if session_id else None,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    await request.app.state.market_cache.set_json(_ticket_key(ticket), payload, ttl_seconds=TICKET_TTL_SECONDS)
    return {"ticket": ticket, "expires_in": TICKET_TTL_SECONDS, "websocket_path": "/ws/events"}


async def _event_frames(request: Request, source: str, value: Any, user_id: str,
                        can_view_market: bool, last_ticks: dict[str, Any],
                        batch: bool = False) -> list[dict[str, Any]]:
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, dict):
        return []

    if source == "signals:new:v1":
        if not can_view_market:
            return []
        full = await _load_signal_shared(request, str(value.get("id") or ""), ("new", value))
        if not full:
            # Avoid violating the contract by publishing a partial signal payload.
            return []
        full["event"] = {"type": "new", "via": "scanner"}
        return [{"channel": "signals", "event": "signal.new", "payload": full, "ts": now_iso}]

    if source == "signals:events:v1":
        if not can_view_market:
            return []
        signal_id = str(value.get("signal_id") or "")
        full = await _load_signal_shared(request, signal_id, ("event", value))
        if not full:
            return []
        raw_type = str(value.get("type") or "")
        if raw_type == "signal.status.updated":
            status = str(value.get("status") or "").upper()
            if status == "TOUCHED":
                event_name, event_type = "signal.touched", "touched"
            elif status == "CANCELLED":
                event_name, event_type = "signal.cancelled", "cancelled"
                full["cancel_reason"] = value.get("reason") or full.get("cancel_reason")
            else:
                return []
            full["event"] = {"type": event_type, "via": "worker"}
            return [{"channel": "signals", "event": event_name, "payload": full, "ts": now_iso}]
        if raw_type == "signal.outcome.updated":
            event_type = str(value.get("event_type") or "").upper()
            result = str(value.get("result") or "").upper()
            if event_type == "TP1_HIT":
                full["operation_result"] = "TP1_HIT"
                full["event"] = {"type": "tp1", "result": "TP1", "r": value.get("r"), "via": "worker"}
                event_name = "signal.tp1"
            elif event_type == "CLOSED" and result in {"TP1", "TP2", "SL"}:
                full["operation_result"] = result
                full["event"] = {"type": "closed", "result": result, "r": value.get("r"), "via": value.get("event_type")}
                event_name = "signal.closed"
            else:
                return []
            return [{"channel": "signals", "event": event_name, "payload": full, "ts": now_iso}]
        return []

    if source == "market:events":
        if not can_view_market:
            return []
        event = str(value.get("event") or "")
        payload = value.get("payload")
        if event == "price.update" and isinstance(payload, list) and batch:
            # One frame per snapshot instead of one frame per symbol, and symbols whose
            # visible fields did not change since the last frame sent to this client
            # are skipped. This removes ~400 websocket sends/s per browser.
            rows = []
            for row in payload:
                if not isinstance(row, dict) or not row.get("symbol"):
                    continue
                symbol = str(row["symbol"]).upper()
                signature = (row.get("price"), row.get("change_pct"), row.get("volume_24h"),
                             row.get("high_24h"), row.get("low_24h"))
                if last_ticks.get(symbol) == signature:
                    continue
                last_ticks[symbol] = signature
                rows.append({
                    "symbol": symbol, "price": row.get("price"), "change_pct": row.get("change_pct"),
                    "volume_24h": row.get("volume_24h"), "high_24h": row.get("high_24h"),
                    "low_24h": row.get("low_24h"),
                })
            if not rows:
                return []
            return [{"channel": "prices", "event": "tick.batch", "payload": rows, "ts": value.get("ts") or now_iso}]
        if event == "price.update" and isinstance(payload, list):
            frames = []
            now = time.monotonic()
            for row in payload:
                if not isinstance(row, dict) or not row.get("symbol"):
                    continue
                symbol = str(row["symbol"]).upper()
                # The worker already emits a market snapshot on Binance's cadence.
                # Do not apply a second per-symbol throttle here: it can discard the
                # newest tick when upstream messages arrive close together.
                last_ticks[symbol] = now
                frames.append({"channel": "prices", "event": "tick", "payload": {
                    "symbol": symbol, "price": row.get("price"), "change_pct": row.get("change_pct"),
                    "volume_24h": row.get("volume_24h"), "high_24h": row.get("high_24h"),
                    "low_24h": row.get("low_24h"),
                }, "ts": value.get("ts") or now_iso})
            return frames
        if event == "funding.update" and isinstance(payload, list):
            frames = []
            for row in payload:
                if not isinstance(row, dict) or not row.get("symbol"):
                    continue
                symbol = str(row["symbol"]).upper()
                rate = row.get("lastFundingRate")
                if not isinstance(rate, (int, float)):
                    try:
                        rate = float(rate)
                    except (TypeError, ValueError, OverflowError):
                        continue
                if not math.isfinite(float(rate)):
                    continue
                frames.append({"channel": "prices", "event": "funding.tick", "payload": {
                    "symbol": symbol, "funding_rate": float(rate), "mark_price": row.get("markPrice"),
                    "index_price": row.get("indexPrice"), "next_funding_time": row.get("nextFundingTime"),
                }, "ts": value.get("ts") or now_iso})
            return frames
        if event == "liquidation.new":
            now_mono = time.monotonic()
            if now_mono - float(last_ticks.get("__liquidation_frame__", 0.0) or 0.0) < LIQUIDATION_FRAME_MIN_INTERVAL:
                return []
            last_ticks["__liquidation_frame__"] = now_mono
            try:
                summary = await request.app.state.market_intelligence_service.liquidations(15)
            except Exception:
                return []
            return [{"channel": "market.liquidations", "event": "summary", "payload": summary, "ts": now_iso}]
        return []

    if source == "notifications:events:v1":
        if str(value.get("user_id") or "") != str(user_id):
            return []
        notification = value.get("notification")
        if not isinstance(notification, dict):
            return []
        # Expired accounts keep the websocket for plan/payment notices so they
        # can renew, but all feature/market notifications are withheld after
        # the trial/subscription boundary. Fail closed for unknown notification
        # kinds rather than accidentally exposing a newly added feature alert.
        kind = str(notification.get("kind") or "vol").lower()
        if not can_view_market and kind != "plans":
            return []
        payload = {
            "id": notification.get("id"),
            "kind": kind,
            "key": notification.get("event_id") or notification.get("id"),
            "text": notification.get("message") or notification.get("title") or "Nueva notificación",
            "symbol": notification.get("symbol"),
            "created_at": notification.get("created_at") or now_iso,
        }
        return [{"channel": "alerts", "event": "alert.new", "payload": payload, "ts": now_iso}]

    if source in {"account:events:v1", "billing:events:v1"}:
        # Account events are private to the account which generated them. An admin
        # does not get other users' account payloads through this personal channel.
        if str(value.get("user_id") or "") != str(user_id):
            return []
        channel = str(value.get("channel") or "account")
        event = str(value.get("event") or "")
        if channel != "account" or event not in {"plan.changed", "ban.changed", "payment.updated"}:
            return []
        payload = value.get("payload", {})
        return [{"channel": "account", "event": event, "payload": payload, "ts": value.get("ts") or now_iso}]

    if source == "market:movers:events:v1" and can_view_market:
        payload = value.get("payload")
        if isinstance(payload, dict):
            return [{"channel": "market.movers", "event": "snapshot", "payload": payload, "ts": value.get("ts") or now_iso}]
    return []


@router.websocket("/ws/events")
async def websocket_events(
    websocket: WebSocket,
    ticket: str = Query(default="", min_length=16, max_length=128),
    batch: int = Query(default=0, ge=0, le=1),
) -> None:
    await websocket.accept()
    request = websocket  # exposes app.state while keeping the transformer typed simply
    cache = websocket.app.state.market_cache
    if not ticket:
        await websocket.close(code=4401, reason="Ticket requerido")
        return
    try:
        ticket_data = await cache.consume_json(_ticket_key(ticket))
    except Exception:
        ticket_data = None
    if not isinstance(ticket_data, dict):
        await websocket.close(code=4401, reason="Ticket inválido o vencido")
        return

    user_id = str(ticket_data.get("user_id") or "")
    session_id = ticket_data.get("session_id")
    auth_repo = websocket.app.state.auth_repository
    try:
        if not user_id or not _session_is_active(auth_repo, str(session_id) if session_id else None, user_id):
            await websocket.close(code=4401, reason="Sesión revocada o vencida")
            return
        user = await asyncio.to_thread(auth_repo.get_user_by_id, user_id)
    except Exception:
        await websocket.close(code=1013, reason="Servicio de sesiones temporalmente no disponible")
        return
    if not user:
        await websocket.close(code=4401, reason="Cuenta inexistente")
        return

    admin = _is_admin(user_id, user)
    access = evaluate_access(user, now=datetime.now(timezone.utc), is_admin=admin)
    can_view_market = bool(access.get("allowed"))
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=500)
    receiver_task: asyncio.Task | None = None
    access_user: Mapping[str, Any] = user
    access_checked_at = time.monotonic()
    prices_enabled = True
    use_batch = bool(batch)
    last_ticks: dict[str, Any] = {}

    def enqueue_event(channel: str, item: Any) -> None:
        """Keep market prices latest-wins so a slow client does not replay stale ticks."""
        is_price_snapshot = (
            channel == "market:events"
            and isinstance(item, dict)
            and item.get("event") == "price.update"
        )
        if is_price_snapshot and not prices_enabled:
            return  # tab hidden: the browser asked to pause the heavy price stream
        if is_price_snapshot:
            # A price.update contains a whole snapshot of changed symbols. If an older
            # snapshot is still waiting, replace it instead of building a stale backlog.
            pending: list[tuple[str, Any]] = []
            while True:
                try:
                    queued = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                old_channel, old_item = queued
                old_is_price_snapshot = (
                    old_channel == "market:events"
                    and isinstance(old_item, dict)
                    and old_item.get("event") == "price.update"
                )
                if not old_is_price_snapshot:
                    pending.append(queued)
                elif use_batch:
                    # Batched frames dedupe against what was last SENT, so merge the
                    # replaced snapshot into the new one instead of losing its symbols.
                    old_rows = old_item.get("payload") if isinstance(old_item, dict) else None
                    new_rows = item.get("payload") if isinstance(item, dict) else None
                    if isinstance(old_rows, list) and isinstance(new_rows, list):
                        newest = {str(r.get("symbol")): r for r in new_rows if isinstance(r, dict)}
                        merged_rows = [r for r in old_rows if isinstance(r, dict) and str(r.get("symbol")) not in newest]
                        item = {**item, "payload": merged_rows + list(newest.values())}
            for queued in pending:
                queue.put_nowait(queued)

        # Price snapshots are replaceable state, while signals/account events are
        # notifications that must not be evicted merely because a price tick arrived.
        # If the queue is full after coalescing old price snapshots, drop this incoming
        # price snapshot rather than removing a non-price event from the queue.
        if queue.full() and is_price_snapshot:
            logger.warning("Se descarta snapshot de precio porque la cola está llena de eventos no reemplazables")
            return

        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait((channel, item))
        except asyncio.QueueFull:
            # Defensive only; this function does not await, so no other task can
            # change the queue between the fullness check and put_nowait.
            logger.warning("Se descartó un evento WebSocket porque la cola está llena (%s)", channel)

    async def receive_controls() -> None:
        """Read small control messages and detect a closed socket immediately.

        `{"op":"prices","enabled":false}` pauses price/funding frames (hidden tab);
        signals, alerts and account events keep flowing. Re-enabling resets the
        per-client dedupe so the next snapshot carries every changed symbol again.
        """
        nonlocal prices_enabled
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                text = message.get("text")
                if not isinstance(text, str) or len(text) > 512:
                    continue
                try:
                    control = json.loads(text)
                except ValueError:
                    continue
                if isinstance(control, dict) and control.get("op") == "prices":
                    enabled = bool(control.get("enabled", True))
                    if enabled and not prices_enabled:
                        last_ticks.clear()
                    prices_enabled = enabled
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass
        finally:
            while True:
                try:
                    queue.put_nowait(("__closed__", None))
                    break
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass

    hub: EventHub | None = getattr(websocket.app.state, "event_hub", None)
    if hub is None or getattr(hub, "_cache", None) is not cache:
        hub = EventHub(cache, EVENT_CHANNELS)
        websocket.app.state.event_hub = hub

    try:
        hub.add(enqueue_event)
        receiver_task = asyncio.create_task(receive_controls(), name="ws-controls")
        while True:
            try:
                source, item = await asyncio.wait_for(queue.get(), timeout=25.0)
            except asyncio.TimeoutError:
                heartbeat = {
                    "channel": "system",
                    "event": "heartbeat",
                    "payload": {},
                    "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
                if not await _try_send_websocket_json(websocket, heartbeat):
                    return
                continue
            if source == "__closed__":
                return

            is_price_source = (
                source == "market:events"
                and isinstance(item, dict)
                and item.get("event") in {"price.update", "funding.update"}
            )
            if is_price_source and not prices_enabled:
                continue
            own_account_event = (
                source in {"account:events:v1", "billing:events:v1", "notifications:events:v1"}
                and isinstance(item, dict)
                and str(item.get("user_id") or "") == user_id
            )

            # Re-check current account restrictions at event time; a ban/expiry takes
            # effect without waiting for the short-lived WS ticket to expire. The
            # account row is cached per connection (see ACCESS_RECHECK_SECONDS) but
            # the entitlement is recomputed from it for every event.
            try:
                if own_account_event or time.monotonic() - access_checked_at >= ACCESS_RECHECK_SECONDS:
                    current_user = await asyncio.to_thread(auth_repo.get_user_by_id, user_id)
                    if not current_user:
                        await websocket.close(code=4401, reason="Cuenta inexistente")
                        return
                    access_user = current_user
                    access_checked_at = time.monotonic()
                can_view_market = bool(evaluate_access(access_user, now=datetime.now(timezone.utc), is_admin=_is_admin(user_id, access_user)).get("allowed"))
                if use_batch:
                    frames = await _event_frames(websocket, source, item, user_id, can_view_market, last_ticks, batch=True)
                else:
                    frames = await _event_frames(websocket, source, item, user_id, can_view_market, last_ticks)
            except Exception:
                # A malformed/unavailable event is isolated; the live connection can
                # still receive subsequent valid events. Transport errors are handled
                # only by _try_send_websocket_json below.
                logger.exception("Fallo al transformar evento WebSocket (%s)", source)
                continue

            for frame in frames:
                if not await _try_send_websocket_json(websocket, frame):
                    return
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Conexión WebSocket interrumpida")
        if (
            websocket.client_state is WebSocketState.CONNECTED
            and websocket.application_state is WebSocketState.CONNECTED
        ):
            try:
                await websocket.close(code=1011, reason="Error interno del canal de eventos")
            except (RuntimeError, OSError, WebSocketDisconnect):
                pass
    finally:
        if receiver_task is not None:
            receiver_task.cancel()
            await asyncio.gather(receiver_task, return_exceptions=True)
        await hub.release(enqueue_event)
