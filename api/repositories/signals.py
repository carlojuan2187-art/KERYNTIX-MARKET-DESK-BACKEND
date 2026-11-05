"""Signal repository adapters: MongoDB for deployment and memory for tests/dev."""
from __future__ import annotations

from api.mongo_config import mongo_pool_options

import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol, Sequence


class SignalRepository(Protocol):
    def list_signals(self, *, limit: int = 100) -> list[Mapping[str, Any]]: ...
    def list_live_candidates(self, *, limit: int = 80, cancelled_since: datetime | None = None) -> list[Mapping[str, Any]]: ...
    def latest_by_symbol(self, symbol: str) -> Mapping[str, Any] | None: ...
    def list_results(self, *, limit: int = 100) -> list[Mapping[str, Any]]: ...
    def list_results_for_period(self, *, start: datetime | None = None) -> list[Mapping[str, Any]]: ...
    def record_user_deliveries(self, user_id: str, signal_ids: Sequence[str], *, delivered_at: datetime) -> None: ...
    def list_user_deliveries(self, user_id: str, *, start: datetime | None = None) -> list[Mapping[str, Any]]: ...
    def list_results_by_signal_ids(self, signal_ids: Sequence[str]) -> list[Mapping[str, Any]]: ...
    def create_signal_if_not_duplicate(
        self, document: Mapping[str, Any], *, dedupe_minutes: int = 10
    ) -> bool: ...
    def health(self) -> dict[str, Any]: ...


class RepositoryUnavailable(RuntimeError):
    """Storage is configured but cannot currently serve a request."""


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _doc_created_at(document: Mapping[str, Any]) -> datetime | None:
    value = document.get("created_at")
    if value is None and isinstance(document.get("lifecycle"), Mapping):
        value = document["lifecycle"].get("created_at")
    return _parse_datetime(value)


class InMemorySignalRepository:
    """Non-persistent adapter for tests and explicit local development only."""

    def __init__(self, documents: Sequence[Mapping[str, Any]] | None = None,
                 results: Sequence[Mapping[str, Any]] | None = None) -> None:
        self._documents = [dict(doc) for doc in (documents or [])]
        self._results = [dict(row) for row in (results or [])]
        self._user_deliveries: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    def list_signals(self, *, limit: int = 100) -> list[Mapping[str, Any]]:
        with self._lock:
            ordered = sorted(
                self._documents,
                key=lambda doc: _doc_created_at(doc) or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            return [dict(doc) for doc in ordered[:limit]]

    def list_live_candidates(self, *, limit: int = 80, cancelled_since: datetime | None = None) -> list[Mapping[str, Any]]:
        cutoff = _parse_datetime(cancelled_since) if cancelled_since is not None else datetime.now(timezone.utc) - timedelta(minutes=30)
        rows = self.list_signals(limit=min(1000, max(1, len(self._documents))))
        visible: list[Mapping[str, Any]] = []
        for row in rows:
            lifecycle = row.get("lifecycle") if isinstance(row.get("lifecycle"), Mapping) else {}
            status = str(row.get("status") or lifecycle.get("status") or "").upper()
            if status in {"PENDING", "TOUCHED"}:
                visible.append(row)
                continue
            if status == "CANCELLED":
                cancelled_at = _parse_datetime(row.get("cancelled_at") or lifecycle.get("cancelled_at") or row.get("created_at"))
                if cancelled_at is not None and cancelled_at >= cutoff:
                    visible.append(row)
                continue
            outcome = row.get("outcome") if isinstance(row.get("outcome"), Mapping) else {}
            if not status and str(outcome.get("state") or "").upper() in {"OPEN", "TP1_HIT"}:
                visible.append(row)
        return visible[:max(1, min(int(limit), 200))]

    def latest_by_symbol(self, symbol: str) -> Mapping[str, Any] | None:
        normalized = symbol.strip().upper()
        for doc in self.list_signals(limit=len(self._documents)):
            if str(doc.get("symbol", "")).upper() == normalized:
                return dict(doc)
        return None

    def create_signal_if_not_duplicate(
        self, document: Mapping[str, Any], *, dedupe_minutes: int = 10
    ) -> bool:
        now = _doc_created_at(document) or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=max(1, dedupe_minutes))
        signature = (str(document.get("symbol", "")).upper(), str(document.get("direction", "")).upper(), str(document.get("visibility", "premium")).lower())
        with self._lock:
            for existing in self._documents:
                old_signature = (str(existing.get("symbol", "")).upper(), str(existing.get("direction", "")).upper(), str(existing.get("visibility", "premium")).lower())
                old_created = _doc_created_at(existing)
                if old_signature != signature:
                    continue
                if str(existing.get("status", "")).upper() == "PENDING" or (old_created is not None and old_created >= cutoff):
                    return False
            self._documents.append(dict(document))
            return True

    def list_results(self, *, limit: int = 100) -> list[Mapping[str, Any]]:
        with self._lock:
            ordered = sorted(self._results, key=lambda row: _parse_datetime(row.get("closed_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return [dict(row) for row in ordered[:max(1, min(int(limit), 500))]]

    def list_results_for_period(self, *, start: datetime | None = None) -> list[Mapping[str, Any]]:
        """Return all results from a UTC lower bound; used for honest all-time stats."""
        cutoff = _parse_datetime(start) if start is not None else None
        with self._lock:
            rows = [dict(row) for row in self._results]
        if cutoff is not None:
            rows = [row for row in rows if (_parse_datetime(row.get("closed_at")) is not None and _parse_datetime(row.get("closed_at")) >= cutoff)]
        return rows

    def record_user_deliveries(self, user_id: str, signal_ids: Sequence[str], *, delivered_at: datetime) -> None:
        moment = _parse_datetime(delivered_at) or datetime.now(timezone.utc)
        with self._lock:
            for raw_id in signal_ids:
                signal_id = str(raw_id or "").strip()
                if signal_id:
                    key = (str(user_id), signal_id)
                    self._user_deliveries.setdefault(key, {
                        "user_id": str(user_id), "signal_id": signal_id, "delivered_at": moment,
                    })

    def list_user_deliveries(self, user_id: str, *, start: datetime | None = None) -> list[Mapping[str, Any]]:
        cutoff = _parse_datetime(start) if start is not None else None
        with self._lock:
            rows = [dict(row) for (owner, _), row in self._user_deliveries.items() if owner == str(user_id)]
        if cutoff is not None:
            rows = [row for row in rows if (_parse_datetime(row.get("delivered_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
        return sorted(rows, key=lambda row: _parse_datetime(row.get("delivered_at")) or datetime.min.replace(tzinfo=timezone.utc))

    def list_results_by_signal_ids(self, signal_ids: Sequence[str]) -> list[Mapping[str, Any]]:
        wanted = {str(value) for value in signal_ids if str(value)}
        with self._lock:
            return [dict(row) for row in self._results if str(row.get("signal_id") or row.get("id") or "") in wanted]

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "adapter": "memory", "persistent": False}


class MongoSignalRepository:
    """MongoDB adapter for persisted signals, including atomic scanner deduplication."""

    def __init__(self, collection: Any, client: Any, *, database_name: str, results_collection: Any | None = None, deliveries_collection: Any | None = None) -> None:
        self._collection = collection
        self._client = client
        self._database_name = database_name
        self._results_collection = (
            results_collection
            if results_collection is not None
            else client[database_name][os.getenv("SIGNAL_RESULTS_COLLECTION", "signal_results")]
        )
        self._deliveries_collection = (
            deliveries_collection
            if deliveries_collection is not None
            else client[database_name][os.getenv("USER_SIGNAL_DELIVERIES_COLLECTION", "user_signal_deliveries")]
        )

    @classmethod
    def from_env(cls) -> "MongoSignalRepository":
        uri = os.getenv("MONGODB_URI")
        database_name = os.getenv("DATABASE_NAME")
        if not uri or not database_name:
            raise RuntimeError("MONGODB_URI y DATABASE_NAME son obligatorios para MongoDB")
        try:
            from pymongo import MongoClient
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError("Instala pymongo con: pip install 'pymongo>=4.7'") from exc
        timeout_ms = max(1000, int(os.getenv("MONGODB_TIMEOUT_MS", "10000")))
        client = MongoClient(
            uri,
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
            socketTimeoutMS=timeout_ms,
            retryWrites=True,
            **mongo_pool_options(),
        )
        db = client[database_name]
        signal_collection = db[os.getenv("SIGNALS_COLLECTION", "signals")]
        result_collection = db[os.getenv("SIGNAL_RESULTS_COLLECTION", "signal_results")]
        deliveries_collection = db[os.getenv("USER_SIGNAL_DELIVERIES_COLLECTION", "user_signal_deliveries")]
        try:
            # These endpoints repeatedly read the newest signal documents. Without
            # this index MongoDB must scan and sort the entire signals collection;
            # its 10s server-selection/socket timeout is longer than the frontend's
            # old 9s request deadline. The compound index also serves latest_by_symbol.
            signal_collection.create_index([("created_at", -1)])
            signal_collection.create_index([("symbol", 1), ("created_at", -1)])
            signal_collection.create_index([("status", 1), ("created_at", -1)])
            signal_collection.create_index([("lifecycle.status", 1), ("created_at", -1)])
            signal_collection.create_index([("outcome.state", 1), ("created_at", -1)])
            result_collection.create_index("signal_id", unique=True)
            result_collection.create_index([("closed_at", -1)])
            deliveries_collection.create_index([("user_id", 1), ("signal_id", 1)], unique=True, name="uniq_user_signal_delivery")
            deliveries_collection.create_index([("user_id", 1), ("delivered_at", 1)], name="user_signal_delivery_time")
        except Exception:
            client.close()
            raise
        return cls(signal_collection, client, database_name=database_name, results_collection=result_collection, deliveries_collection=deliveries_collection)

    def _query(self, cursor: Any) -> list[Mapping[str, Any]]:
        try:
            return [dict(doc) for doc in cursor]
        except Exception as exc:
            self._raise_storage_error(exc)

    @staticmethod
    def _raise_storage_error(exc: Exception) -> None:
        try:
            from pymongo.errors import PyMongoError
        except ImportError:
            PyMongoError = Exception  # type: ignore[misc,assignment]
        if isinstance(exc, PyMongoError):
            raise RepositoryUnavailable("MongoDB no está disponible") from exc
        raise exc

    def list_signals(self, *, limit: int = 100) -> list[Mapping[str, Any]]:
        try:
            cursor = self._collection.find({}).sort("created_at", -1).limit(max(1, min(limit, 1000)))
            return self._query(cursor)
        except Exception as exc:
            self._raise_storage_error(exc)

    def list_live_candidates(self, *, limit: int = 80, cancelled_since: datetime | None = None) -> list[Mapping[str, Any]]:
        """Fetch only records that can appear in the live signal feed.

        This avoids reading and normalizing hundreds of already-closed records on
        every page visit. Root and lifecycle status paths are both supported for
        legacy bot documents.
        """
        cutoff = _parse_datetime(cancelled_since) if cancelled_since is not None else datetime.now(timezone.utc) - timedelta(minutes=30)
        query = {"$or": [
            {"status": {"$in": ["PENDING", "TOUCHED"]}},
            {"lifecycle.status": {"$in": ["PENDING", "TOUCHED"]}},
            {"outcome.state": {"$in": ["OPEN", "TP1_HIT"]}, "status": {"$exists": False}},
            {"status": "CANCELLED", "cancelled_at": {"$gte": cutoff}},
            {"lifecycle.status": "CANCELLED", "lifecycle.cancelled_at": {"$gte": cutoff}},
            {"status": "CANCELLED", "cancelled_at": {"$exists": False}, "created_at": {"$gte": cutoff}},
            {"status": "CANCELLED", "cancelled_at": None, "created_at": {"$gte": cutoff}},
            {"lifecycle.status": "CANCELLED", "lifecycle.cancelled_at": None, "created_at": {"$gte": cutoff}},
        ]}
        try:
            cursor = (
                self._collection.find(query)
                .sort("created_at", -1)
                .limit(max(1, min(int(limit), 200)))
                .max_time_ms(3500)
            )
            return self._query(cursor)
        except Exception as exc:
            self._raise_storage_error(exc)

    def latest_by_symbol(self, symbol: str) -> Mapping[str, Any] | None:
        try:
            value = self._collection.find_one({"symbol": symbol.strip().upper()}, sort=[("created_at", -1)])
            return dict(value) if value else None
        except Exception as exc:
            self._raise_storage_error(exc)

    def list_results(self, *, limit: int = 100) -> list[Mapping[str, Any]]:
        try:
            cursor = self._results_collection.find({}).sort("closed_at", -1).limit(max(1, min(int(limit), 500)))
            rows = []
            for raw in cursor:
                item = dict(raw)
                item.pop("_id", None)
                if not item.get("id"):
                    item["id"] = str(item.get("signal_id") or "")
                rows.append(item)
            return rows
        except Exception as exc:
            self._raise_storage_error(exc)

    def list_results_for_period(self, *, start: datetime | None = None) -> list[Mapping[str, Any]]:
        """Read all closed-result snapshots from a UTC lower bound (no arbitrary 500 cap)."""
        query: dict[str, Any] = {}
        if start is not None:
            query["closed_at"] = {"$gte": start}
        try:
            cursor = self._results_collection.find(query).sort("closed_at", 1)
            rows = []
            for raw in cursor:
                item = dict(raw)
                item.pop("_id", None)
                if not item.get("id"):
                    item["id"] = str(item.get("signal_id") or "")
                rows.append(item)
            return rows
        except Exception as exc:
            self._raise_storage_error(exc)

    def record_user_deliveries(self, user_id: str, signal_ids: Sequence[str], *, delivered_at: datetime) -> None:
        moment = _parse_datetime(delivered_at) or datetime.now(timezone.utc)
        for raw_id in dict.fromkeys(str(value).strip() for value in signal_ids if str(value).strip()):
            try:
                self._deliveries_collection.update_one(
                    {"user_id": str(user_id), "signal_id": raw_id},
                    {"$setOnInsert": {"user_id": str(user_id), "signal_id": raw_id, "delivered_at": moment}},
                    upsert=True,
                )
            except Exception as exc:
                # The feed and notification worker can record the same signal at
                # almost the same instant. The unique index makes that idempotent;
                # a concurrent duplicate insert is therefore success, not an outage.
                if getattr(exc, "code", None) == 11000:
                    continue
                self._raise_storage_error(exc)

    def list_user_deliveries(self, user_id: str, *, start: datetime | None = None) -> list[Mapping[str, Any]]:
        query: dict[str, Any] = {"user_id": str(user_id)}
        if start is not None:
            query["delivered_at"] = {"$gte": _parse_datetime(start) or start}
        try:
            rows = []
            for raw in self._deliveries_collection.find(query).sort("delivered_at", 1):
                item = dict(raw)
                item.pop("_id", None)
                rows.append(item)
            return rows
        except Exception as exc:
            self._raise_storage_error(exc)

    def list_results_by_signal_ids(self, signal_ids: Sequence[str]) -> list[Mapping[str, Any]]:
        ids = list(dict.fromkeys(str(value) for value in signal_ids if str(value)))
        if not ids:
            return []
        try:
            rows = []
            for raw in self._results_collection.find({"signal_id": {"$in": ids}}):
                item = dict(raw)
                item.pop("_id", None)
                if not item.get("id"):
                    item["id"] = str(item.get("signal_id") or "")
                rows.append(item)
            return rows
        except Exception as exc:
            self._raise_storage_error(exc)

    def create_signal_if_not_duplicate(
        self, document: Mapping[str, Any], *, dedupe_minutes: int = 10
    ) -> bool:
        """Check current pending/cooldown signals and reserve a signature atomically.

        The short-lived reservation collection prevents concurrent scanner processes
        from inserting the same symbol/direction/visibility at the same time. The
        cooldown reservation is conservative: if insertion fails, it is released.
        """
        try:
            from pymongo import ReturnDocument
            from pymongo.errors import DuplicateKeyError
            from pymongo.errors import PyMongoError
        except ImportError as exc:  # pragma: no cover
            raise RepositoryUnavailable("Instala pymongo para escribir señales") from exc

        now = _doc_created_at(document) or datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=max(1, dedupe_minutes))
        symbol = str(document.get("symbol", "")).upper()
        direction = str(document.get("direction", "")).upper()
        visibility = str(document.get("visibility", "premium")).lower()
        try:
            existing = self._collection.find_one({
                "symbol": symbol,
                "direction": direction,
                "visibility": visibility,
                "$or": [{"status": "PENDING"}, {"created_at": {"$gte": cutoff}}],
            }, {"_id": 1})
            if existing:
                return False

            dedupe = self._client[self._database_name]["_ftrade_signal_dedup"]
            dedupe.create_index("key", unique=True)
            key = f"{symbol}|{direction}|{visibility}"
            signal_id = str(document.get("id") or document.get("signal_id") or "")
            cooldown_until = now + timedelta(minutes=max(1, dedupe_minutes))
            try:
                reservation = dedupe.find_one_and_update(
                    {
                        "key": key,
                        "$or": [
                            {"cooldown_until": {"$lte": now}},
                            {"cooldown_until": {"$exists": False}},
                        ],
                    },
                    {"$set": {"cooldown_until": cooldown_until, "signal_id": signal_id, "updated_at": now},
                     "$setOnInsert": {"key": key}},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                # Existing reservation is still within its cooldown.
                return False
            if not reservation or reservation.get("signal_id") != signal_id:
                return False
            try:
                self._collection.insert_one(dict(document))
                return True
            except Exception:
                dedupe.update_one({"key": key, "signal_id": signal_id}, {"$set": {"cooldown_until": now}})
                raise
        except RepositoryUnavailable:
            raise
        except PyMongoError as exc:
            self._raise_storage_error(exc)

    def health(self) -> dict[str, Any]:
        try:
            self._client.admin.command("ping")
            return {"status": "ok", "adapter": "mongodb", "persistent": True, "database": self._database_name}
        except Exception as exc:
            self._raise_storage_error(exc)
            return {"status": "error", "adapter": "mongodb", "persistent": True}

    def close(self) -> None:
        self._client.close()


def repository_from_environment() -> SignalRepository:
    """Do not silently switch to memory in production when Mongo is misconfigured."""
    environment = os.getenv("FTRADE_ENV", "development").strip().lower()
    uri = os.getenv("MONGODB_URI")
    if uri:
        return MongoSignalRepository.from_env()
    if environment == "production":
        raise RuntimeError("FTRADE_ENV=production requiere MONGODB_URI y DATABASE_NAME")
    return InMemorySignalRepository()
