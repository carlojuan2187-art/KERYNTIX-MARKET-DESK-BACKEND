"""Process-wide fan-out of Redis pub/sub events to WebSocket connections.

Before this hub every browser connection opened its own Redis pub/sub connection per
channel (7 per client) and decoded every JSON message again. With N browsers that
meant 7*N Redis connections and N JSON decodes of the same price snapshot every
second. The hub keeps ONE subscription per channel for the whole API process, decodes
each message once and hands the same (read-only) object to every connected client.

The subscriptions only exist while at least one client is connected, so idle servers
hold no Redis connections and tests do not leave background tasks behind.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Iterable

logger = logging.getLogger("ftrade.events.hub")

Listener = Callable[[str, Any], None]


class EventHub:
    def __init__(self, cache: Any, channels: Iterable[str]) -> None:
        self._cache = cache
        self._channels = tuple(channels)
        self._listeners: set[Listener] = set()
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def listener_count(self) -> int:
        return len(self._listeners)

    def add(self, listener: Listener) -> None:
        loop = asyncio.get_running_loop()
        if self._tasks and self._loop is not loop:
            # Tasks created on a previous (closed) event loop can never deliver again.
            self._tasks = []
            self._listeners.clear()
        if not self._tasks:
            self._loop = loop
            self._tasks = [
                loop.create_task(self._pump(channel), name=f"ws-hub-{channel}")
                for channel in self._channels
            ]
        self._listeners.add(listener)

    async def release(self, listener: Listener) -> None:
        self._listeners.discard(listener)
        if self._listeners or not self._tasks:
            return
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _pump(self, channel: str) -> None:
        delay = 0.5
        while True:
            try:
                async for item in self._cache.subscribe_json(channel):
                    delay = 0.5
                    for listener in tuple(self._listeners):
                        try:
                            listener(channel, item)
                        except Exception:  # one broken client must not stop the others
                            logger.exception("Listener de eventos falló (%s)", channel)
                # The iterator ending is unexpected; resubscribe instead of going silent.
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Suscripción %s interrumpida (%s: %s); reintentando", channel, type(exc).__name__, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 10.0)
