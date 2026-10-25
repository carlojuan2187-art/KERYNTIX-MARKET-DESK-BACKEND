"""Async JSON cache adapters for shared market state and event publication."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import defaultdict, deque
from typing import Any, AsyncIterator, Protocol


class JsonCache(Protocol):
    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None: ...
    async def append_json(self, key: str, value: Any, *, max_length: int, ttl_seconds: int | None = None) -> None: ...
    async def get_list_json(self, key: str, *, limit: int = 100) -> list[Any]: ...
    async def append_timed_json(self, key: str, value: Any, *, timestamp: float, max_length: int, retention_seconds: int) -> None: ...
    async def get_timed_list_json(self, key: str, *, limit: int = 100, retention_seconds: int = 7200) -> list[Any]: ...
    async def publish_json(self, channel: str, value: Any) -> int: ...
    async def consume_json(self, key: str) -> Any | None: ...
    def subscribe_json(self, channel: str) -> AsyncIterator[Any]: ...
    async def acquire_lease(self, key: str, token: str, *, ttl_seconds: int) -> bool: ...
    async def renew_lease(self, key: str, token: str, *, ttl_seconds: int) -> bool: ...
    async def release_lease(self, key: str, token: str) -> bool: ...
    async def close(self) -> None: ...
    async def health(self) -> dict[str, Any]: ...


class InMemoryJsonCache:
    """In-process cache for unit tests and single-process local development only."""

    def __init__(self) -> None:
        self._values: dict[str, tuple[Any, float | None]] = {}
        self._lists: dict[str, deque[tuple[Any, float]]] = defaultdict(deque)
        self._timed_lists: dict[str, deque[tuple[Any, float]]] = defaultdict(deque)
        self.published: list[tuple[str, Any]] = []
        self._leases: dict[str, tuple[str, float]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[Any]]] = defaultdict(set)

    def _read_value(self, key: str) -> Any | None:
        record = self._values.get(key)
        if record is None:
            return None
        value, expires_at = record
        if expires_at is not None and expires_at <= time.time():
            self._values.pop(key, None)
            return None
        # Use JSON round-trip to mimic the serialization behavior of Redis.
        return json.loads(json.dumps(value, allow_nan=False))

    async def get_json(self, key: str) -> Any | None:
        return self._read_value(key)

    async def set_json(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        expires_at = time.time() + ttl_seconds if ttl_seconds and ttl_seconds > 0 else None
        self._values[key] = (json.loads(encoded), expires_at)

    async def append_json(self, key: str, value: Any, *, max_length: int, ttl_seconds: int | None = None) -> None:
        queue = self._lists[key]
        queue.appendleft((json.loads(json.dumps(value, allow_nan=False)), time.time() + ttl_seconds if ttl_seconds else 0))
        while len(queue) > max(1, max_length):
            queue.pop()

    async def get_list_json(self, key: str, *, limit: int = 100) -> list[Any]:
        now = time.time()
        queue = self._lists[key]
        while queue and queue[-1][1] and queue[-1][1] <= now:
            queue.pop()
        return [json.loads(json.dumps(item)) for item, _expires in list(queue)[:max(0, limit)]]

    async def append_timed_json(self, key: str, value: Any, *, timestamp: float, max_length: int, retention_seconds: int) -> None:
        now = time.time()
        queue = self._timed_lists[key]
        queue.appendleft((json.loads(json.dumps(value, allow_nan=False)), float(timestamp)))
        cutoff = now - max(1, retention_seconds)
        retained = sorted(((item, score) for item, score in queue if score >= cutoff), key=lambda pair: pair[1], reverse=True)
        retained = retained[:max(1, max_length)]
        queue.clear()
        queue.extend(retained)

    async def get_timed_list_json(self, key: str, *, limit: int = 100, retention_seconds: int = 7200) -> list[Any]:
        now = time.time()
        cutoff = now - max(1, retention_seconds)
        queue = self._timed_lists[key]
        retained = sorted(((item, score) for item, score in queue if score >= cutoff), key=lambda pair: pair[1], reverse=True)
        queue.clear()
        queue.extend(retained)
        return [json.loads(json.dumps(item)) for item, _score in list(queue)[:max(0, limit)]]

    async def publish_json(self, channel: str, value: Any) -> int:
        cloned = json.loads(json.dumps(value, allow_nan=False))
        self.published.append((channel, cloned))
        subscribers = list(self._subscribers.get(channel, set()))
        for queue in subscribers:
            # Keep local development memory bounded; when a consumer is slow, drop
            # its oldest queued event instead of allowing unbounded growth.
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(cloned)
            except asyncio.QueueFull:
                pass
        return len(subscribers)

    async def consume_json(self, key: str) -> Any | None:
        record = self._values.pop(key, None)
        if record is None:
            return None
        value, expires_at = record
        if expires_at is not None and expires_at <= time.time():
            return None
        return json.loads(json.dumps(value, allow_nan=False))

    async def subscribe_json(self, channel: str) -> AsyncIterator[Any]:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1000)
        self._subscribers[channel].add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers[channel].discard(queue)
            if not self._subscribers[channel]:
                self._subscribers.pop(channel, None)

    async def acquire_lease(self, key: str, token: str, *, ttl_seconds: int) -> bool:
        # Single-event-loop atomicity is sufficient for deterministic local tests;
        # this adapter is deliberately not a distributed coordination mechanism.
        now = time.monotonic()
        existing = self._leases.get(key)
        if existing and existing[1] > now:
            return existing[0] == token
        self._leases[key] = (str(token), now + max(1, int(ttl_seconds)))
        return True

    async def renew_lease(self, key: str, token: str, *, ttl_seconds: int) -> bool:
        now = time.monotonic()
        existing = self._leases.get(key)
        if not existing or existing[0] != str(token) or existing[1] <= now:
            return False
        self._leases[key] = (str(token), now + max(1, int(ttl_seconds)))
        return True

    async def release_lease(self, key: str, token: str) -> bool:
        existing = self._leases.get(key)
        if not existing or existing[0] != str(token):
            return False
        self._leases.pop(key, None)
        return True

    async def close(self) -> None:
        return None

    async def health(self) -> dict[str, Any]:
        return {"status": "ok", "adapter": "memory", "shared": False}


class RedisJsonCache:
    """Shared Redis cache. Imports redis lazily so unit tests need no Redis service."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str) -> "RedisJsonCache":
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - depends on deployment dependencies
            raise RuntimeError("Instala redis con: pip install 'redis>=5,<7'") from exc
        return cls(Redis.from_url(url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3, health_check_interval=30))

    async def get_json(self, key: str) -> Any | None:
        raw = await self._client.get(key)
        return None if raw is None else json.loads(raw)

    async def set_json(self, key: str, value: Any, *, ttl_seconds: int | None = None) -> None:
        raw = json.dumps(value, allow_nan=False, separators=(",", ":"))
        if ttl_seconds and ttl_seconds > 0:
            await self._client.set(key, raw, ex=ttl_seconds)
        else:
            await self._client.set(key, raw)

    async def append_json(self, key: str, value: Any, *, max_length: int, ttl_seconds: int | None = None) -> None:
        pipe = self._client.pipeline(transaction=True)
        pipe.lpush(key, json.dumps(value, allow_nan=False, separators=(",", ":")))
        pipe.ltrim(key, 0, max(0, max_length - 1))
        if ttl_seconds and ttl_seconds > 0:
            pipe.expire(key, ttl_seconds)
        await pipe.execute()

    async def get_list_json(self, key: str, *, limit: int = 100) -> list[Any]:
        if limit <= 0:
            return []
        values = await self._client.lrange(key, 0, limit - 1)
        return [json.loads(value) for value in values]

    async def append_timed_json(self, key: str, value: Any, *, timestamp: float, max_length: int, retention_seconds: int) -> None:
        # Redis sorted sets provide both retention-by-time and a hard length cap without
        # rereading up to 200k liquidations on every incoming event.
        raw = json.dumps(value, allow_nan=False, separators=(",", ":"))
        member = f"{float(timestamp):.6f}|{uuid.uuid4().hex}|{raw}"
        client = self._client
        pipe = client.pipeline(transaction=True)
        cutoff = time.time() - max(1, retention_seconds)
        pipe.zadd(key, {member: float(timestamp)})
        pipe.zremrangebyscore(key, "-inf", cutoff)
        pipe.expire(key, max(60, retention_seconds * 2))
        await pipe.execute()
        size = int(await client.zcard(key))
        if size > max_length:
            await client.zremrangebyrank(key, 0, size - max_length - 1)

    async def get_timed_list_json(self, key: str, *, limit: int = 100, retention_seconds: int = 7200) -> list[Any]:
        if limit <= 0:
            return []
        cutoff = time.time() - max(1, retention_seconds)
        await self._client.zremrangebyscore(key, "-inf", cutoff)
        members = await self._client.zrevrange(key, 0, limit - 1)
        out: list[Any] = []
        for member in members:
            # Sorted-set member: timestamp|unique-id|JSON payload.
            pieces = str(member).split("|", 2)
            if len(pieces) == 3:
                out.append(json.loads(pieces[2]))
        return out

    async def publish_json(self, channel: str, value: Any) -> int:
        raw = json.dumps(value, allow_nan=False, separators=(",", ":"))
        return int(await self._client.publish(channel, raw))

    async def consume_json(self, key: str) -> Any | None:
        # Redis GETDEL makes one-time WebSocket ticket consumption atomic across API
        # replicas; no other connection can successfully reuse the ticket.
        raw = await self._client.getdel(key)
        return None if raw is None else json.loads(raw)

    async def subscribe_json(self, channel: str) -> AsyncIterator[Any]:
        pubsub = self._client.pubsub()
        await pubsub.subscribe(channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message.get("data")
                try:
                    yield json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode("utf-8"))
                except (ValueError, TypeError, AttributeError):
                    continue
        finally:
            try:
                await pubsub.unsubscribe(channel)
            finally:
                closer = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
                if closer:
                    outcome = closer()
                    if hasattr(outcome, "__await__"):
                        await outcome

    async def acquire_lease(self, key: str, token: str, *, ttl_seconds: int) -> bool:
        return bool(await self._client.set(key, str(token), nx=True, ex=max(1, int(ttl_seconds))))

    async def renew_lease(self, key: str, token: str, *, ttl_seconds: int) -> bool:
        # Compare-and-expire prevents one process from renewing another process's lease.
        script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
        return bool(await self._client.eval(script, 1, key, str(token), max(1, int(ttl_seconds))))

    async def release_lease(self, key: str, token: str) -> bool:
        # Compare-and-delete prevents a stale worker from deleting a replacement lease.
        script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
        return bool(await self._client.eval(script, 1, key, str(token)))

    async def close(self) -> None:
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def health(self) -> dict[str, Any]:
        try:
            await self._client.ping()
            return {"status": "ok", "adapter": "redis", "shared": True}
        except Exception:
            return {"status": "error", "adapter": "redis", "shared": True}


def cache_from_environment() -> JsonCache:
    """Production requires shared Redis; memory is never a silent production fallback."""
    url = os.getenv("REDIS_URL")
    environment = os.getenv("FTRADE_ENV", "development").strip().lower()
    if url:
        return RedisJsonCache.from_url(url)
    if environment == "production":
        raise RuntimeError("FTRADE_ENV=production requiere REDIS_URL para compartir el feed entre worker y API")
    return InMemoryJsonCache()
