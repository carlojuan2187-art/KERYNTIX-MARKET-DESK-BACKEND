"""Real MongoDB/Redis smoke tests.

These tests are intentionally skipped in the ordinary unit suite. Run them only
against disposable integration services with FTRADE_INTEGRATION=1.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("FTRADE_INTEGRATION") != "1",
        reason="Requires disposable real MongoDB and Redis services; set FTRADE_INTEGRATION=1",
    ),
]


def test_mongodb_payment_transaction_claim_is_durable_and_idempotent():
    from api.repositories.business import MongoBusinessRepository

    repo = MongoBusinessRepository.from_env()
    txid = f"integration:{uuid.uuid4().hex}"
    try:
        first = repo.claim_payment_tx(txid, {"payment_id": "payment-a", "user_id": "user-a"})
        retry_same_payment = repo.claim_payment_tx(txid, {"payment_id": "payment-a", "user_id": "user-a"})
        conflicting_payment = repo.claim_payment_tx(txid, {"payment_id": "payment-b", "user_id": "user-b"})

        assert first is True
        assert retry_same_payment is True
        assert conflicting_payment is False
        stored = repo.get_payment_tx(txid)
        assert stored is not None
        assert stored["payment_id"] == "payment-a"

        names = {item["name"] for item in repo._payment_txs.list_indexes()}
        assert "uniq_payment_txid" in names
    finally:
        repo.delete_payment_tx(txid)
        repo.close()


def test_redis_json_roundtrip_and_distributed_lease_scripts():
    from api.cache import RedisJsonCache

    async def scenario():
        cache = RedisJsonCache.from_url(os.environ["REDIS_URL"])
        key = f"ftrade:integration:{uuid.uuid4().hex}"
        lease_key = f"ftrade:integration-lease:{uuid.uuid4().hex}"
        try:
            assert (await cache.health())["status"] == "ok"
            await cache.set_json(key, {"state": "persisted", "count": 3}, ttl_seconds=60)
            assert await cache.get_json(key) == {"state": "persisted", "count": 3}

            assert await cache.acquire_lease(lease_key, "owner-a", ttl_seconds=30) is True
            assert await cache.acquire_lease(lease_key, "owner-b", ttl_seconds=30) is False
            assert await cache.renew_lease(lease_key, "owner-b", ttl_seconds=30) is False
            assert await cache.renew_lease(lease_key, "owner-a", ttl_seconds=30) is True
            assert await cache.release_lease(lease_key, "owner-b") is False
            assert await cache.release_lease(lease_key, "owner-a") is True
            assert await cache.acquire_lease(lease_key, "owner-b", ttl_seconds=30) is True
        finally:
            await cache._client.delete(key, lease_key)
            await cache.close()

    asyncio.run(scenario())
