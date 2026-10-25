from __future__ import annotations

import asyncio

from api.cache import InMemoryJsonCache
from worker.distributed_lease import DistributedWorkerLease


def test_lease_only_owner_can_renew_or_release():
    async def scenario():
        cache = InMemoryJsonCache()
        assert await cache.acquire_lease("worker:test", "owner-a", ttl_seconds=60) is True
        assert await cache.acquire_lease("worker:test", "owner-b", ttl_seconds=60) is False
        assert await cache.renew_lease("worker:test", "owner-b", ttl_seconds=60) is False
        assert await cache.release_lease("worker:test", "owner-b") is False
        assert await cache.renew_lease("worker:test", "owner-a", ttl_seconds=60) is True
        assert await cache.release_lease("worker:test", "owner-a") is True
        assert await cache.acquire_lease("worker:test", "owner-b", ttl_seconds=60) is True
    asyncio.run(scenario())


def test_worker_lease_publishes_running_then_stopped_status():
    async def scenario():
        cache = InMemoryJsonCache()
        async with DistributedWorkerLease(cache, "unit-test-worker", ttl_seconds=15, retry_seconds=1):
            status = await cache.get_json("ftrade:worker-status:development:unit-test-worker:v1")
            assert status["status"] == "running"
            assert status["worker"] == "unit-test-worker"
        status = await cache.get_json("ftrade:worker-status:development:unit-test-worker:v1")
        assert status["status"] == "stopped"
    asyncio.run(scenario())
