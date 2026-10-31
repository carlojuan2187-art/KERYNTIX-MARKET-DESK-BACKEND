"""Storage for short-lived observed liquidity sweeps used by the strategy engine."""
from __future__ import annotations

from api.mongo_config import mongo_pool_options

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Protocol

RETENTION_MINUTES = max(1, int(os.getenv("SWEEP_OBSERVATION_RETENTION_MINUTES", "20")))


def _utc(value: Any) -> datetime | None:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SweepStore(Protocol):
    def load_active(self, *, now: datetime | None = None) -> dict[str, dict[str, Any]]: ...
    def save_observations(self, observations: Iterable[Mapping[str, Any]]) -> int: ...
    def health(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class InMemorySweepStore:
    """Process-local store for deterministic tests and isolated local runs."""
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def load_active(self, *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        now = _utc(now) or datetime.now(timezone.utc)
        with self._lock:
            self._items = {
                key: item for key, item in self._items.items()
                if (_utc(item.get("expires_at")) or datetime.min.replace(tzinfo=timezone.utc)) > now
            }
            return {key: dict(value) for key, value in self._items.items()}

    def save_observations(self, observations: Iterable[Mapping[str, Any]]) -> int:
        saved = 0
        with self._lock:
            for raw in observations:
                item = dict(raw)
                key = str(item.get("_id") or "")
                event_at = _utc(item.get("event_at"))
                if not key or event_at is None:
                    continue
                existing = self._items.get(key)
                if existing and (_utc(existing.get("event_at")) or datetime.min.replace(tzinfo=timezone.utc)) > event_at:
                    continue
                self._items[key] = item
                saved += 1
        return saved

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "adapter": "memory", "persistent": False}

    def close(self) -> None:
        return None


class MongoSweepStore:
    def __init__(self, collection: Any, client: Any, *, database_name: str) -> None:
        self._collection = collection
        self._client = client
        self._database_name = database_name

    @classmethod
    def from_env(cls) -> "MongoSweepStore":
        uri, name = os.getenv("MONGODB_URI"), os.getenv("DATABASE_NAME")
        if not uri or not name:
            raise RuntimeError("MONGODB_URI y DATABASE_NAME son obligatorios para persistir barridos")
        try:
            from pymongo import MongoClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Instala pymongo para usar el almacenamiento de barridos") from exc
        timeout_ms = max(1000, int(os.getenv("MONGODB_TIMEOUT_MS", "10000")))
        client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms, connectTimeoutMS=timeout_ms, socketTimeoutMS=timeout_ms, **mongo_pool_options())
        db = client[name]
        collection = db[os.getenv("SWEEP_OBSERVATIONS_COLLECTION", "observed_sweeps")]
        try:
            collection.create_index("expires_at", expireAfterSeconds=0)
            collection.create_index("event_at")
        except Exception:
            client.close()
            raise
        return cls(collection, client, database_name=name)

    def load_active(self, *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
        now = _utc(now) or datetime.now(timezone.utc)
        try:
            docs = self._collection.find({"expires_at": {"$gt": now}})
            return {str(item["_id"]): dict(item) for item in docs}
        except Exception as exc:
            self._raise(exc)

    def save_observations(self, observations: Iterable[Mapping[str, Any]]) -> int:
        try:
            saved = 0
            unique: dict[str, dict[str, Any]] = {}
            for raw in observations:
                item = dict(raw)
                key = str(item.get("_id") or "")
                event_at = _utc(item.get("event_at"))
                if not key or event_at is None:
                    continue
                item["event_at"] = event_at
                old = unique.get(key)
                if old is None or event_at > (_utc(old.get("event_at")) or datetime.min.replace(tzinfo=timezone.utc)):
                    unique[key] = item
            for key, item in unique.items():
                prior = self._collection.find_one({"_id": key}, {"event_at": 1})
                if prior and (_utc(prior.get("event_at")) or datetime.min.replace(tzinfo=timezone.utc)) > item["event_at"]:
                    continue
                self._collection.replace_one({"_id": key}, item, upsert=True)
                saved += 1
            return saved
        except Exception as exc:
            self._raise(exc)

    def health(self) -> dict[str, Any]:
        try:
            self._client.admin.command("ping")
            return {"status": "ok", "adapter": "mongodb", "persistent": True, "database": self._database_name}
        except Exception as exc:
            self._raise(exc)

    @staticmethod
    def _raise(exc: Exception) -> None:
        try:
            from pymongo.errors import PyMongoError
        except ImportError:
            PyMongoError = Exception  # type: ignore[misc,assignment]
        if isinstance(exc, PyMongoError):
            raise RuntimeError("MongoDB no está disponible para los barridos observados") from exc
        raise exc

    def close(self) -> None:
        self._client.close()


def sweep_store_from_environment() -> SweepStore:
    if os.getenv("MONGODB_URI"):
        return MongoSweepStore.from_env()
    if os.getenv("FTRADE_ENV", "development").strip().lower() == "production":
        raise RuntimeError("FTRADE_ENV=production requiere MongoDB para los barridos observados")
    return InMemorySweepStore()
