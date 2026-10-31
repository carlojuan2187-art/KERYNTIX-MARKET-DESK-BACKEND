"""Persistence adapters for signal lifecycle transitions and immutable results."""
from __future__ import annotations

from api.mongo_config import mongo_pool_options

import os
import threading
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from engine.lifecycle_logic import STATUS_CANCELLED, STATUS_PENDING, STATUS_TOUCHED, transition_update, utc_naive

OPEN_STATES = ("OPEN", "TP1_HIT")


def _id_value(signal: Mapping[str, Any]) -> str:
    return str(signal.get("id") or signal.get("signal_id") or signal.get("_id") or "")


def _status(signal: Mapping[str, Any]) -> str:
    lifecycle = signal.get("lifecycle") or {}
    return str(signal.get("status") or lifecycle.get("status") or "").upper()


def _set_dotted(target: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = target
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


class LifecycleRepository(Protocol):
    def list_pending(self, *, limit: int = 2000) -> list[Mapping[str, Any]]: ...
    def list_open(self, *, limit: int = 2000) -> list[Mapping[str, Any]]: ...
    def transition_pending(self, signal: Mapping[str, Any], event: Mapping[str, Any]) -> bool: ...
    def save_result_if_absent(self, result: Mapping[str, Any]) -> bool: ...
    def compare_and_set_outcome(
        self, signal: Mapping[str, Any], previous_tracked_until: datetime | None,
        outcome: Mapping[str, Any], previous_live_price_at: datetime | None = None,
    ) -> bool: ...
    def list_results(self, *, limit: int = 100) -> list[Mapping[str, Any]]: ...
    def health(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class InMemoryLifecycleRepository:
    """Thread-safe lifecycle persistence for unit tests and single-process development."""

    def __init__(self, documents: Sequence[Mapping[str, Any]] | None = None,
                 results: Sequence[Mapping[str, Any]] | None = None) -> None:
        self._documents = [dict(doc) for doc in (documents or [])]
        self._results = {str(row.get("signal_id") or row.get("id")): dict(row) for row in (results or [])}
        self._lock = threading.RLock()

    def _find_index(self, signal: Mapping[str, Any]) -> int | None:
        wanted = _id_value(signal)
        for index, row in enumerate(self._documents):
            if _id_value(row) == wanted:
                return index
            # In-memory fixtures may use _id independently from canonical id.
            if signal.get("_id") is not None and row.get("_id") == signal.get("_id"):
                return index
        return None

    def list_pending(self, *, limit: int = 2000) -> list[Mapping[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self._documents if _status(row) == STATUS_PENDING]
        rows.sort(key=lambda row: utc_naive(row.get("created_at") or (row.get("lifecycle") or {}).get("created_at")) or datetime.min, reverse=False)
        return rows[:max(1, min(int(limit), 5000))]

    def list_open(self, *, limit: int = 2000) -> list[Mapping[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self._documents
                    if _status(row) == STATUS_TOUCHED
                    and str((row.get("outcome") or {}).get("state") or "OPEN").upper() in OPEN_STATES]
        rows.sort(key=lambda row: utc_naive((row.get("outcome") or {}).get("tracked_until") or row.get("touched_at")) or datetime.min)
        return rows[:max(1, min(int(limit), 5000))]

    def transition_pending(self, signal: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
        new_status = str(event.get("status") or "").upper()
        fields = transition_update(dict(signal), dict(event))
        if not fields or new_status not in {STATUS_TOUCHED, STATUS_CANCELLED}:
            return False
        with self._lock:
            index = self._find_index(signal)
            if index is None or _status(self._documents[index]) != STATUS_PENDING:
                return False
            row = self._documents[index]
            for path, value in fields.items():
                _set_dotted(row, path, value)
            return True

    def save_result_if_absent(self, result: Mapping[str, Any]) -> bool:
        key = str(result.get("signal_id") or result.get("id") or "")
        if not key:
            return False
        with self._lock:
            if key in self._results:
                return False
            self._results[key] = dict(result)
            return True

    def compare_and_set_outcome(self, signal: Mapping[str, Any], previous_tracked_until: datetime | None,
                                outcome: Mapping[str, Any], previous_live_price_at: datetime | None = None) -> bool:
        with self._lock:
            index = self._find_index(signal)
            if index is None:
                return False
            row = self._documents[index]
            if _status(row) != STATUS_TOUCHED:
                return False
            old_outcome = row.get("outcome") or {}
            actual = utc_naive(old_outcome.get("tracked_until"))
            expected = utc_naive(previous_tracked_until)
            if actual != expected:
                return False
            actual_live = utc_naive(old_outcome.get("last_live_price_at"))
            expected_live = utc_naive(previous_live_price_at)
            if actual_live != expected_live:
                return False
            for key, value in outcome.items():
                if key == "recipient_ids":
                    continue
                row.setdefault("outcome", {})[key] = value
            if outcome.get("result") in {"TP1", "TP2", "SL"}:
                row["operation_result"] = outcome["result"]
            if outcome.get("r_real") is not None:
                row["r_result"] = outcome["r_real"]
            return True

    def list_results(self, *, limit: int = 100) -> list[Mapping[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self._results.values()]
        rows.sort(key=lambda row: utc_naive(row.get("closed_at")) or datetime.min, reverse=True)
        return rows[:max(1, min(int(limit), 500))]

    def snapshot_signals(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._documents]

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "adapter": "memory", "persistent": False}

    def close(self) -> None:
        return None


class MongoLifecycleRepository:
    """MongoDB-backed compare-and-set transitions and immutable result records."""

    def __init__(self, signals: Any, results: Any, client: Any, *, database_name: str) -> None:
        self._signals = signals
        self._results = results
        self._client = client
        self._database_name = database_name

    @classmethod
    def from_env(cls) -> "MongoLifecycleRepository":
        uri = os.getenv("MONGODB_URI")
        database_name = os.getenv("DATABASE_NAME")
        if not uri or not database_name:
            raise RuntimeError("MONGODB_URI y DATABASE_NAME son obligatorios para el monitor de señales")
        try:
            from pymongo import MongoClient
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError("Instala pymongo>=4.7 para persistencia de ciclo de vida") from exc
        timeout_ms = max(1000, int(os.getenv("MONGODB_TIMEOUT_MS", "10000")))
        client = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms,
                             connectTimeoutMS=timeout_ms, socketTimeoutMS=timeout_ms, retryWrites=True,
                             **mongo_pool_options())
        db = client[database_name]
        signal_collection = db[os.getenv("SIGNALS_COLLECTION", "signals")]
        result_collection = db[os.getenv("SIGNAL_RESULTS_COLLECTION", "signal_results")]
        try:
            signal_collection.create_index([("status", 1), ("created_at", 1)])
            signal_collection.create_index([("status", 1), ("outcome.state", 1), ("outcome.tracked_until", 1)])
            result_collection.create_index("signal_id", unique=True)
            result_collection.create_index([("closed_at", -1)])
        except Exception:
            client.close()
            raise
        return cls(signal_collection, result_collection, client, database_name=database_name)

    @staticmethod
    def _selector(signal: Mapping[str, Any]) -> dict[str, Any]:
        if signal.get("_id") is not None:
            return {"_id": signal["_id"]}
        key = _id_value(signal)
        return {"id": key}

    @staticmethod
    def _clean(doc: Mapping[str, Any]) -> dict[str, Any]:
        row = dict(doc)
        # MongoDB's ObjectId isn't part of the public contract; preserve it for CAS,
        # but callers can still serialize the canonical string `id` independently.
        return row

    def list_pending(self, *, limit: int = 2000) -> list[Mapping[str, Any]]:
        try:
            cursor = self._signals.find({"status": STATUS_PENDING}).sort("created_at", 1).limit(max(1, min(int(limit), 5000)))
            return [self._clean(row) for row in cursor]
        except Exception as exc:
            self._raise_storage(exc)

    def list_open(self, *, limit: int = 2000) -> list[Mapping[str, Any]]:
        try:
            cursor = self._signals.find({"status": STATUS_TOUCHED, "outcome.state": {"$in": list(OPEN_STATES)}}).sort("outcome.tracked_until", 1).limit(max(1, min(int(limit), 5000)))
            return [self._clean(row) for row in cursor]
        except Exception as exc:
            self._raise_storage(exc)

    def transition_pending(self, signal: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
        fields = transition_update(dict(signal), dict(event))
        if not fields:
            return False
        selector = self._selector(signal)
        selector["status"] = STATUS_PENDING
        try:
            result = self._signals.update_one(selector, {"$set": fields})
            return result.modified_count == 1
        except Exception as exc:
            self._raise_storage(exc)
            return False

    def save_result_if_absent(self, result: Mapping[str, Any]) -> bool:
        document = dict(result)
        key = str(document.get("signal_id") or document.get("id") or "")
        if not key:
            return False
        document["signal_id"] = key
        try:
            from pymongo.errors import DuplicateKeyError
            insert = self._results.update_one({"signal_id": key}, {"$setOnInsert": document}, upsert=True)
            return getattr(insert, "upserted_id", None) is not None
        except Exception as exc:
            try:
                from pymongo.errors import DuplicateKeyError
                if isinstance(exc, DuplicateKeyError):
                    return False
            except ImportError:
                pass
            self._raise_storage(exc)
            return False

    def compare_and_set_outcome(self, signal: Mapping[str, Any], previous_tracked_until: datetime | None,
                                outcome: Mapping[str, Any], previous_live_price_at: datetime | None = None) -> bool:
        selector = self._selector(signal)
        selector["status"] = STATUS_TOUCHED
        expected = utc_naive(previous_tracked_until)
        if expected is None:
            selector["$or"] = [
                {"outcome.tracked_until": None},
                {"outcome.tracked_until": {"$exists": False}},
            ]
        else:
            selector["outcome.tracked_until"] = expected
        expected_live = utc_naive(previous_live_price_at)
        if expected_live is None:
            selector["$and"] = [{"$or": [
                {"outcome.last_live_price_at": None},
                {"outcome.last_live_price_at": {"$exists": False}},
            ]}]
        else:
            selector["outcome.last_live_price_at"] = expected_live
        fields = {f"outcome.{key}": value for key, value in outcome.items() if key != "recipient_ids"}
        if outcome.get("result") in {"TP1", "TP2", "SL"}:
            fields["operation_result"] = outcome["result"]
        if outcome.get("r_real") is not None:
            fields["r_result"] = outcome["r_real"]
        try:
            result = self._signals.update_one(selector, {"$set": fields})
            return result.modified_count == 1
        except Exception as exc:
            self._raise_storage(exc)
            return False

    def list_results(self, *, limit: int = 100) -> list[Mapping[str, Any]]:
        try:
            cursor = self._results.find({}).sort("closed_at", -1).limit(max(1, min(int(limit), 500)))
            rows = []
            for raw in cursor:
                item = dict(raw)
                item.pop("_id", None)
                if not item.get("id"):
                    item["id"] = str(item.get("signal_id") or "")
                rows.append(item)
            return rows
        except Exception as exc:
            self._raise_storage(exc)

    def health(self) -> dict[str, Any]:
        try:
            self._client.admin.command("ping")
            return {"status": "ok", "adapter": "mongodb", "persistent": True, "database": self._database_name}
        except Exception as exc:
            self._raise_storage(exc)
            return {"status": "error", "adapter": "mongodb", "persistent": True}

    @staticmethod
    def _raise_storage(exc: Exception) -> None:
        try:
            from pymongo.errors import PyMongoError
        except ImportError:
            PyMongoError = Exception  # type: ignore[misc,assignment]
        if isinstance(exc, PyMongoError):
            raise RuntimeError("MongoDB no está disponible para el ciclo de vida de señales") from exc
        raise exc

    def close(self) -> None:
        self._client.close()


def lifecycle_repository_from_environment() -> LifecycleRepository:
    if os.getenv("MONGODB_URI"):
        return MongoLifecycleRepository.from_env()
    if os.getenv("FTRADE_ENV", "development").strip().lower() == "production":
        raise RuntimeError("FTRADE_ENV=production requiere MongoDB para el ciclo de vida de señales")
    if os.getenv("LIFECYCLE_ALLOW_MEMORY", "false").lower() in {"1", "true", "yes"}:
        return InMemoryLifecycleRepository()
    raise RuntimeError("Configura MONGODB_URI o activa LIFECYCLE_ALLOW_MEMORY=true para pruebas aisladas")
