"""Shared, bounded MongoDB connection-pool settings for every repository."""
from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def mongo_pool_options() -> dict[str, int]:
    """Return conservative PyMongo pool limits, with safe environment clamps.

    The options are passed to every MongoClient factory so API and worker
    processes do not inherit PyMongo's much larger default pool independently.
    """
    max_pool = max(5, min(100, _int_env("MONGODB_MAX_POOL_SIZE", 20)))
    min_pool = max(0, min(max_pool, 10, _int_env("MONGODB_MIN_POOL_SIZE", 0)))
    max_connecting = max(1, min(8, _int_env("MONGODB_MAX_CONNECTING", 4)))
    wait_queue_timeout = max(1000, min(30000, _int_env("MONGODB_WAIT_QUEUE_TIMEOUT_MS", 5000)))
    max_idle_time = max(10000, min(300000, _int_env("MONGODB_MAX_IDLE_TIME_MS", 60000)))
    return {
        "maxPoolSize": max_pool,
        "minPoolSize": min_pool,
        "maxConnecting": max_connecting,
        "waitQueueTimeoutMS": wait_queue_timeout,
        "maxIdleTimeMS": max_idle_time,
    }
