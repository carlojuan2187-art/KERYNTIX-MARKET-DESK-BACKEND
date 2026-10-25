"""Persistence for user price alerts and the in-app notification inbox.

MongoDB is mandatory in production. The in-memory adapter is deterministic and
intended only for unit tests/local development.
"""
from __future__ import annotations

import os
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return None


def _clean(doc: Any) -> dict[str, Any] | None:
    if not isinstance(doc, Mapping):
        return None
    out = dict(doc)
    oid = out.pop("_id", None)
    if oid is not None and not out.get("id"):
        out["id"] = str(oid)
    return out


class UserFeatureRepository(Protocol):
    def create_price_alert(self, document: Mapping[str, Any]) -> dict[str, Any]: ...
    def get_price_alert(self, alert_id: str, user_id: str) -> dict[str, Any] | None: ...
    def list_price_alerts(self, user_id: str, *, enabled_only: bool = False, limit: int = 100) -> list[dict[str, Any]]: ...
    def list_enabled_price_alerts(self, *, limit: int = 5000) -> list[dict[str, Any]]: ...
    def update_price_alert(self, alert_id: str, user_id: str, fields: Mapping[str, Any]) -> dict[str, Any] | None: ...
    def delete_price_alert(self, alert_id: str, user_id: str) -> bool: ...
    def claim_price_alert_trigger(self, alert_id: str, *, now: datetime) -> dict[str, Any] | None: ...
    def create_notification(self, document: Mapping[str, Any]) -> tuple[dict[str, Any], bool]: ...
    def claim_telegram_delivery(self, *, now: datetime, lease_until: datetime, claim_id: str) -> dict[str, Any] | None: ...
    def finish_telegram_delivery(self, notification_id: str, claim_id: str, *, outcome: str,
                                 now: datetime, next_attempt_at: datetime | None = None,
                                 error_code: str | None = None) -> bool: ...
    def create_or_update_push_subscription(self, document: Mapping[str, Any]) -> dict[str, Any]: ...
    def list_push_subscriptions(self, user_id: str, *, limit: int = 10) -> list[dict[str, Any]]: ...
    def active_push_subscription_count(self, user_id: str) -> int: ...
    def delete_push_subscription(self, subscription_id: str, user_id: str) -> bool: ...
    def delete_push_subscription_by_id(self, subscription_id: str) -> bool: ...
    def claim_web_push_delivery(self, *, now: datetime, lease_until: datetime, claim_id: str) -> dict[str, Any] | None: ...
    def record_web_push_endpoint_result(self, notification_id: str, claim_id: str, subscription_id: str,
                                        result: Mapping[str, Any], *, now: datetime) -> bool: ...
    def finish_web_push_delivery(self, notification_id: str, claim_id: str, *, outcome: str,
                                 now: datetime, next_attempt_at: datetime | None = None,
                                 error_code: str | None = None) -> bool: ...
    def list_notifications(self, user_id: str, *, unread_only: bool = False, limit: int = 50, before: datetime | None = None) -> list[dict[str, Any]]: ...
    def unread_count(self, user_id: str) -> int: ...
    def mark_notification_read(self, notification_id: str, user_id: str, *, now: datetime) -> dict[str, Any] | None: ...
    def mark_all_notifications_read(self, user_id: str, *, now: datetime) -> int: ...
    def delete_notification(self, notification_id: str, user_id: str) -> bool: ...
    def health(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class InMemoryUserFeatureRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._alerts: dict[str, dict[str, Any]] = {}
        self._notifications: dict[str, dict[str, Any]] = {}
        self._notification_keys: dict[tuple[str, str], str] = {}
        self._push_subscriptions: dict[str, dict[str, Any]] = {}
        self._push_endpoint_ids: dict[str, str] = {}

    def create_price_alert(self, document: Mapping[str, Any]) -> dict[str, Any]:
        doc = deepcopy(dict(document))
        alert_id = str(doc.setdefault("id", uuid.uuid4().hex))
        with self._lock:
            for row in self._alerts.values():
                if (row.get("user_id") == doc.get("user_id") and row.get("enabled")
                        and row.get("symbol") == doc.get("symbol")
                        and row.get("condition") == doc.get("condition")
                        and float(row.get("target_price", 0)) == float(doc.get("target_price", 0))):
                    raise ValueError("DUPLICATE_ACTIVE_PRICE_ALERT")
            self._alerts[alert_id] = doc
            return deepcopy(doc)

    def get_price_alert(self, alert_id: str, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._alerts.get(str(alert_id))
            return deepcopy(row) if row and str(row.get("user_id")) == str(user_id) else None

    def list_price_alerts(self, user_id: str, *, enabled_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = [deepcopy(row) for row in self._alerts.values()
                    if str(row.get("user_id")) == str(user_id) and (not enabled_only or row.get("enabled"))]
        rows.sort(key=lambda row: _aware(row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows[:max(1, min(int(limit), 500))]

    def list_enabled_price_alerts(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        with self._lock:
            rows = [deepcopy(row) for row in self._alerts.values() if row.get("enabled")]
        rows.sort(key=lambda row: _aware(row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc))
        return rows[:max(1, min(int(limit), 10000))]

    def update_price_alert(self, alert_id: str, user_id: str, fields: Mapping[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            row = self._alerts.get(str(alert_id))
            if not row or str(row.get("user_id")) != str(user_id):
                return None
            update = deepcopy(dict(fields))
            reset_triggered = bool(update.pop("reset_triggered", False))
            if update.get("enabled") is True:
                for other_id, other in self._alerts.items():
                    if other_id == str(alert_id) or not other.get("enabled"):
                        continue
                    if (str(other.get("user_id")) == str(user_id)
                            and other.get("symbol") == row.get("symbol")
                            and other.get("condition") == row.get("condition")
                            and float(other.get("target_price", 0)) == float(row.get("target_price", 0))):
                        raise ValueError("DUPLICATE_ACTIVE_PRICE_ALERT")
            row.update(update)
            if reset_triggered:
                row.pop("triggered_at", None)
                row.pop("last_triggered_at", None)
                row["trigger_count"] = 0
            row["updated_at"] = _now()
            return deepcopy(row)

    def delete_price_alert(self, alert_id: str, user_id: str) -> bool:
        with self._lock:
            row = self._alerts.get(str(alert_id))
            if not row or str(row.get("user_id")) != str(user_id):
                return False
            del self._alerts[str(alert_id)]
            return True

    def claim_price_alert_trigger(self, alert_id: str, *, now: datetime) -> dict[str, Any] | None:
        with self._lock:
            row = self._alerts.get(str(alert_id))
            if not row or not row.get("enabled"):
                return None
            last = _aware(row.get("last_triggered_at"))
            if not row.get("repeat") and row.get("triggered_at"):
                return None
            cooldown = max(60, int(row.get("cooldown_seconds", 300)))
            if row.get("repeat") and last and last + timedelta(seconds=cooldown) > now:
                return None
            row["last_triggered_at"] = now
            row["trigger_count"] = int(row.get("trigger_count", 0)) + 1
            if not row.get("repeat"):
                row["triggered_at"] = now
                row["enabled"] = False
            row["updated_at"] = now
            return deepcopy(row)

    def create_notification(self, document: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        doc = deepcopy(dict(document))
        doc.setdefault("id", uuid.uuid4().hex)
        key = (str(doc["user_id"]), str(doc["event_id"]))
        with self._lock:
            previous_id = self._notification_keys.get(key)
            if previous_id:
                return deepcopy(self._notifications[previous_id]), False
            self._notifications[str(doc["id"])] = doc
            self._notification_keys[key] = str(doc["id"])
            return deepcopy(doc), True

    def claim_telegram_delivery(self, *, now: datetime, lease_until: datetime, claim_id: str) -> dict[str, Any] | None:
        """Atomically claim the oldest due Telegram outbox row (in-memory test adapter)."""
        eligible: list[tuple[datetime, dict[str, Any]]] = []
        with self._lock:
            for row in self._notifications.values():
                delivery = row.get("telegram_delivery")
                if not isinstance(delivery, dict):
                    continue
                state = delivery.get("status")
                next_at = _aware(delivery.get("next_attempt_at"))
                lease_at = _aware(delivery.get("lease_until"))
                if state in {"pending", "retry"} and (next_at is None or next_at <= now):
                    eligible.append((_aware(row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), row))
                elif state == "sending" and lease_at is not None and lease_at <= now:
                    eligible.append((_aware(row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), row))
            if not eligible:
                return None
            eligible.sort(key=lambda pair: pair[0])
            row = eligible[0][1]
            delivery = row["telegram_delivery"]
            delivery["status"] = "sending"
            delivery["claim_id"] = str(claim_id)
            delivery["lease_until"] = lease_until
            delivery["updated_at"] = now
            delivery["attempts"] = int(delivery.get("attempts") or 0) + 1
            return deepcopy(row)

    def finish_telegram_delivery(self, notification_id: str, claim_id: str, *, outcome: str,
                                 now: datetime, next_attempt_at: datetime | None = None,
                                 error_code: str | None = None) -> bool:
        if outcome not in {"sent", "retry", "dead", "suppressed"}:
            raise ValueError("INVALID_TELEGRAM_DELIVERY_OUTCOME")
        with self._lock:
            row = self._notifications.get(str(notification_id))
            if not row:
                return False
            delivery = row.get("telegram_delivery")
            if not isinstance(delivery, dict) or delivery.get("status") != "sending" or delivery.get("claim_id") != str(claim_id):
                return False
            delivery["status"] = outcome
            delivery["updated_at"] = now
            delivery.pop("claim_id", None)
            delivery.pop("lease_until", None)
            if outcome == "sent":
                delivery["sent_at"] = now
                delivery.pop("next_attempt_at", None)
                delivery.pop("last_error_code", None)
            elif outcome == "retry":
                delivery["next_attempt_at"] = next_attempt_at or now
                if error_code:
                    delivery["last_error_code"] = str(error_code)[:100]
            elif outcome in {"dead", "suppressed"}:
                delivery["finished_at"] = now
                delivery.pop("next_attempt_at", None)
                if error_code:
                    delivery["last_error_code"] = str(error_code)[:100]
            return True

    def create_or_update_push_subscription(self, document: Mapping[str, Any]) -> dict[str, Any]:
        doc = deepcopy(dict(document))
        endpoint_hash = str(doc.get("endpoint_hash") or "")
        if not endpoint_hash:
            raise ValueError("PUSH_ENDPOINT_HASH_REQUIRED")
        with self._lock:
            previous_id = self._push_endpoint_ids.get(endpoint_hash)
            if previous_id and previous_id in self._push_subscriptions:
                existing = self._push_subscriptions[previous_id]
                # A fresh authenticated registration transfers this browser endpoint
                # to the current account, preventing delivery to a previously signed-in user.
                doc["id"] = previous_id
                doc["created_at"] = existing.get("created_at") or doc.get("created_at")
                self._push_subscriptions[previous_id] = deepcopy(doc)
                return deepcopy(doc)
            subscription_id = str(doc.setdefault("id", uuid.uuid4().hex))
            self._push_subscriptions[subscription_id] = deepcopy(doc)
            self._push_endpoint_ids[endpoint_hash] = subscription_id
            return deepcopy(doc)

    def list_push_subscriptions(self, user_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = [deepcopy(row) for row in self._push_subscriptions.values()
                    if str(row.get("user_id")) == str(user_id) and row.get("active", True)]
        rows.sort(key=lambda row: _aware(row.get("updated_at") or row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows[:max(1, min(int(limit), 50))]

    def active_push_subscription_count(self, user_id: str) -> int:
        with self._lock:
            return sum(1 for row in self._push_subscriptions.values()
                       if str(row.get("user_id")) == str(user_id) and row.get("active", True))

    def delete_push_subscription(self, subscription_id: str, user_id: str) -> bool:
        with self._lock:
            row = self._push_subscriptions.get(str(subscription_id))
            if not row or str(row.get("user_id")) != str(user_id):
                return False
            self._push_subscriptions.pop(str(subscription_id), None)
            if self._push_endpoint_ids.get(str(row.get("endpoint_hash"))) == str(subscription_id):
                self._push_endpoint_ids.pop(str(row.get("endpoint_hash")), None)
            return True

    def delete_push_subscription_by_id(self, subscription_id: str) -> bool:
        with self._lock:
            row = self._push_subscriptions.pop(str(subscription_id), None)
            if not row:
                return False
            if self._push_endpoint_ids.get(str(row.get("endpoint_hash"))) == str(subscription_id):
                self._push_endpoint_ids.pop(str(row.get("endpoint_hash")), None)
            return True

    def claim_web_push_delivery(self, *, now: datetime, lease_until: datetime, claim_id: str) -> dict[str, Any] | None:
        eligible: list[tuple[datetime, dict[str, Any]]] = []
        with self._lock:
            for row in self._notifications.values():
                delivery = row.get("web_push_delivery")
                if not isinstance(delivery, dict):
                    continue
                state = delivery.get("status")
                next_at = _aware(delivery.get("next_attempt_at"))
                lease_at = _aware(delivery.get("lease_until"))
                if state in {"pending", "retry"} and (next_at is None or next_at <= now):
                    eligible.append((_aware(row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), row))
                elif state == "sending" and lease_at is not None and lease_at <= now:
                    eligible.append((_aware(row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), row))
            if not eligible:
                return None
            eligible.sort(key=lambda pair: pair[0])
            row = eligible[0][1]
            delivery = row["web_push_delivery"]
            delivery["status"] = "sending"
            delivery["claim_id"] = str(claim_id)
            delivery["lease_until"] = lease_until
            delivery["updated_at"] = now
            delivery["attempts"] = int(delivery.get("attempts") or 0) + 1
            return deepcopy(row)

    def record_web_push_endpoint_result(self, notification_id: str, claim_id: str, subscription_id: str,
                                        result: Mapping[str, Any], *, now: datetime) -> bool:
        with self._lock:
            row = self._notifications.get(str(notification_id))
            if not row:
                return False
            delivery = row.get("web_push_delivery")
            if not isinstance(delivery, dict) or delivery.get("status") != "sending" or delivery.get("claim_id") != str(claim_id):
                return False
            endpoint_results = delivery.setdefault("endpoint_results", {})
            endpoint_results[str(subscription_id)] = {**deepcopy(dict(result)), "updated_at": now}
            delivery["updated_at"] = now
            return True

    def finish_web_push_delivery(self, notification_id: str, claim_id: str, *, outcome: str,
                                 now: datetime, next_attempt_at: datetime | None = None,
                                 error_code: str | None = None) -> bool:
        if outcome not in {"sent", "retry", "dead", "suppressed"}:
            raise ValueError("INVALID_WEB_PUSH_DELIVERY_OUTCOME")
        with self._lock:
            row = self._notifications.get(str(notification_id))
            if not row:
                return False
            delivery = row.get("web_push_delivery")
            if not isinstance(delivery, dict) or delivery.get("status") != "sending" or delivery.get("claim_id") != str(claim_id):
                return False
            delivery["status"] = outcome
            delivery["updated_at"] = now
            delivery.pop("claim_id", None)
            delivery.pop("lease_until", None)
            if outcome == "sent":
                delivery["sent_at"] = now
                delivery.pop("next_attempt_at", None)
                delivery.pop("last_error_code", None)
            elif outcome == "retry":
                delivery["next_attempt_at"] = next_attempt_at or now
                if error_code:
                    delivery["last_error_code"] = str(error_code)[:100]
            else:
                delivery["finished_at"] = now
                delivery.pop("next_attempt_at", None)
                if error_code:
                    delivery["last_error_code"] = str(error_code)[:100]
            return True

    def list_notifications(self, user_id: str, *, unread_only: bool = False, limit: int = 50, before: datetime | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = []
            for row in self._notifications.values():
                if str(row.get("user_id")) != str(user_id) or not row.get("in_app", True):
                    continue
                if unread_only and row.get("read_at") is not None:
                    continue
                created = _aware(row.get("created_at"))
                if before is not None and (created is None or created >= before):
                    continue
                rows.append(deepcopy(row))
        rows.sort(key=lambda row: _aware(row.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows[:max(1, min(int(limit), 100))]

    def unread_count(self, user_id: str) -> int:
        with self._lock:
            return sum(1 for row in self._notifications.values()
                       if str(row.get("user_id")) == str(user_id) and row.get("in_app", True) and row.get("read_at") is None)

    def mark_notification_read(self, notification_id: str, user_id: str, *, now: datetime) -> dict[str, Any] | None:
        with self._lock:
            row = self._notifications.get(str(notification_id))
            if not row or str(row.get("user_id")) != str(user_id) or not row.get("in_app", True):
                return None
            row["read_at"] = row.get("read_at") or now
            return deepcopy(row)

    def mark_all_notifications_read(self, user_id: str, *, now: datetime) -> int:
        count = 0
        with self._lock:
            for row in self._notifications.values():
                if str(row.get("user_id")) == str(user_id) and row.get("in_app", True) and row.get("read_at") is None:
                    row["read_at"] = now
                    count += 1
        return count

    def delete_notification(self, notification_id: str, user_id: str) -> bool:
        with self._lock:
            row = self._notifications.get(str(notification_id))
            if not row or str(row.get("user_id")) != str(user_id):
                return False
            delivery = row.get("telegram_delivery")
            if isinstance(delivery, dict) and delivery.get("status") in {"pending", "retry", "sending"}:
                # Hiding an inbox item must not discard its durable Telegram job.
                row["in_app"] = False
                row["deleted_at"] = _now()
                return True
            self._notifications.pop(str(notification_id), None)
            self._notification_keys.pop((str(user_id), str(row.get("event_id"))), None)
            return True

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "adapter": "memory", "persistent": False}

    def close(self) -> None:
        return None


class MongoUserFeatureRepository:
    def __init__(self, *, client: Any, db: Any) -> None:
        self._client = client
        self._db = db
        self._notifications = db[os.getenv("USER_NOTIFICATIONS_COLLECTION", "user_notifications")]
        self._price_alerts = db[os.getenv("PRICE_ALERTS_COLLECTION", "price_alerts")]
        self._push_subscriptions = db[os.getenv("WEB_PUSH_SUBSCRIPTIONS_COLLECTION", "web_push_subscriptions")]
        self._ensure_indexes()

    @classmethod
    def from_env(cls) -> "MongoUserFeatureRepository":
        uri = os.getenv("MONGODB_URI")
        database = os.getenv("DATABASE_NAME")
        if not uri or not database:
            raise RuntimeError("MONGODB_URI y DATABASE_NAME son obligatorios para persistir alertas y notificaciones")
        try:
            from pymongo import MongoClient
        except ImportError as exc:
            raise RuntimeError("Instala pymongo para usar MongoDB") from exc
        client = MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
        return cls(client=client, db=client[database])

    def _ensure_indexes(self) -> None:
        self._notifications.create_index([("user_id", 1), ("event_id", 1)], unique=True, name="user_event_unique")
        self._notifications.create_index([("user_id", 1), ("in_app", 1), ("created_at", -1)], name="notification_inbox")
        self._notifications.create_index(
            [("telegram_delivery.status", 1), ("telegram_delivery.next_attempt_at", 1), ("telegram_delivery.lease_until", 1), ("created_at", 1)],
            name="telegram_delivery_outbox_due",
        )
        self._notifications.create_index(
            [("web_push_delivery.status", 1), ("web_push_delivery.next_attempt_at", 1), ("web_push_delivery.lease_until", 1), ("created_at", 1)],
            name="web_push_delivery_outbox_due",
        )
        self._push_subscriptions.create_index([("id", 1)], unique=True, name="web_push_subscription_id_unique")
        self._push_subscriptions.create_index([("endpoint_hash", 1)], unique=True, name="web_push_endpoint_unique")
        self._push_subscriptions.create_index([("user_id", 1), ("active", 1), ("updated_at", -1)], name="web_push_user_active")
        self._price_alerts.create_index([("id", 1)], unique=True, name="price_alert_id_unique")
        self._price_alerts.create_index([("user_id", 1), ("created_at", -1)], name="price_alert_user_created")
        self._price_alerts.create_index([("enabled", 1), ("updated_at", 1)], name="price_alert_dispatch")
        self._price_alerts.create_index([("user_id", 1), ("symbol", 1), ("condition", 1), ("target_price", 1)],
                                        unique=True, partialFilterExpression={"enabled": True}, name="unique_active_price_alert")

    def create_price_alert(self, document: Mapping[str, Any]) -> dict[str, Any]:
        from pymongo.errors import DuplicateKeyError
        doc = dict(document)
        try:
            self._price_alerts.insert_one(doc)
        except DuplicateKeyError as exc:
            raise ValueError("DUPLICATE_ACTIVE_PRICE_ALERT") from exc
        return doc

    def get_price_alert(self, alert_id: str, user_id: str) -> dict[str, Any] | None:
        return _clean(self._price_alerts.find_one({"id": str(alert_id), "user_id": str(user_id)}))

    def list_price_alerts(self, user_id: str, *, enabled_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"user_id": str(user_id)}
        if enabled_only:
            query["enabled"] = True
        rows = self._price_alerts.find(query).sort("created_at", -1).limit(max(1, min(int(limit), 500)))
        return [_clean(row) or {} for row in rows]

    def list_enabled_price_alerts(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        rows = self._price_alerts.find({"enabled": True}).sort("created_at", 1).limit(max(1, min(int(limit), 10000)))
        return [_clean(row) or {} for row in rows]

    def update_price_alert(self, alert_id: str, user_id: str, fields: Mapping[str, Any]) -> dict[str, Any] | None:
        from pymongo import ReturnDocument
        update = dict(fields)
        reset_triggered = bool(update.pop("reset_triggered", False))
        update["updated_at"] = _now()
        operators: dict[str, Any] = {"$set": update}
        if reset_triggered:
            operators["$unset"] = {"triggered_at": "", "last_triggered_at": ""}
            operators["$set"]["trigger_count"] = 0
        return _clean(self._price_alerts.find_one_and_update(
            {"id": str(alert_id), "user_id": str(user_id)}, operators, return_document=ReturnDocument.AFTER,
        ))

    def delete_price_alert(self, alert_id: str, user_id: str) -> bool:
        return self._price_alerts.delete_one({"id": str(alert_id), "user_id": str(user_id)}).deleted_count == 1

    def claim_price_alert_trigger(self, alert_id: str, *, now: datetime) -> dict[str, Any] | None:
        from pymongo import ReturnDocument
        row = self._price_alerts.find_one({"id": str(alert_id), "enabled": True})
        if not row:
            return None
        cooldown = max(60, int(row.get("cooldown_seconds", 300)))
        if row.get("repeat"):
            query = {"id": str(alert_id), "enabled": True, "$or": [
                {"last_triggered_at": {"$exists": False}},
                {"last_triggered_at": {"$lte": now - timedelta(seconds=cooldown)}},
            ]}
            update = {"$set": {"last_triggered_at": now, "updated_at": now}, "$inc": {"trigger_count": 1}}
        else:
            query = {"id": str(alert_id), "enabled": True, "triggered_at": {"$exists": False}}
            update = {"$set": {"last_triggered_at": now, "triggered_at": now, "enabled": False, "updated_at": now}, "$inc": {"trigger_count": 1}}
        return _clean(self._price_alerts.find_one_and_update(query, update, return_document=ReturnDocument.AFTER))

    def create_notification(self, document: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        from pymongo.errors import DuplicateKeyError
        doc = dict(document)
        try:
            self._notifications.insert_one(doc)
            return doc, True
        except DuplicateKeyError:
            existing = self._notifications.find_one({"user_id": str(doc["user_id"]), "event_id": str(doc["event_id"])})
            return (_clean(existing) or doc), False

    def claim_telegram_delivery(self, *, now: datetime, lease_until: datetime, claim_id: str) -> dict[str, Any] | None:
        from pymongo import ReturnDocument

        due_or_unleased = {
            "$and": [
                {"telegram_delivery.status": {"$in": ["pending", "retry"]}},
                {"$or": [
                    {"telegram_delivery.next_attempt_at": {"$exists": False}},
                    {"telegram_delivery.next_attempt_at": {"$lte": now}},
                ]},
            ]
        }
        expired_lease = {
            "$and": [
                {"telegram_delivery.status": "sending"},
                {"telegram_delivery.lease_until": {"$lte": now}},
            ]
        }
        return _clean(self._notifications.find_one_and_update(
            {"$or": [due_or_unleased, expired_lease]},
            {"$set": {
                "telegram_delivery.status": "sending",
                "telegram_delivery.claim_id": str(claim_id),
                "telegram_delivery.lease_until": lease_until,
                "telegram_delivery.updated_at": now,
            }, "$inc": {"telegram_delivery.attempts": 1}},
            sort=[("created_at", 1)], return_document=ReturnDocument.AFTER,
        ))

    def finish_telegram_delivery(self, notification_id: str, claim_id: str, *, outcome: str,
                                 now: datetime, next_attempt_at: datetime | None = None,
                                 error_code: str | None = None) -> bool:
        if outcome not in {"sent", "retry", "dead", "suppressed"}:
            raise ValueError("INVALID_TELEGRAM_DELIVERY_OUTCOME")
        set_fields: dict[str, Any] = {
            "telegram_delivery.status": outcome,
            "telegram_delivery.updated_at": now,
        }
        unset_fields: dict[str, str] = {
            "telegram_delivery.claim_id": "",
            "telegram_delivery.lease_until": "",
        }
        if outcome == "sent":
            set_fields["telegram_delivery.sent_at"] = now
            unset_fields["telegram_delivery.next_attempt_at"] = ""
            unset_fields["telegram_delivery.last_error_code"] = ""
        elif outcome == "retry":
            set_fields["telegram_delivery.next_attempt_at"] = next_attempt_at or now
            if error_code:
                set_fields["telegram_delivery.last_error_code"] = str(error_code)[:100]
        else:
            set_fields["telegram_delivery.finished_at"] = now
            unset_fields["telegram_delivery.next_attempt_at"] = ""
            if error_code:
                set_fields["telegram_delivery.last_error_code"] = str(error_code)[:100]
        result = self._notifications.update_one(
            {"id": str(notification_id), "telegram_delivery.status": "sending", "telegram_delivery.claim_id": str(claim_id)},
            {"$set": set_fields, "$unset": unset_fields},
        )
        return bool(result.modified_count)

    def create_or_update_push_subscription(self, document: Mapping[str, Any]) -> dict[str, Any]:
        from pymongo import ReturnDocument
        doc = dict(document)
        endpoint_hash = str(doc.get("endpoint_hash") or "")
        if not endpoint_hash:
            raise ValueError("PUSH_ENDPOINT_HASH_REQUIRED")
        new_id = str(doc.pop("id", uuid.uuid4().hex))
        created_at = doc.pop("created_at", _now())
        doc.pop("_id", None)
        doc["updated_at"] = doc.get("updated_at") or _now()
        doc["active"] = True
        result = self._push_subscriptions.find_one_and_update(
            {"endpoint_hash": endpoint_hash},
            {"$set": doc, "$setOnInsert": {"id": new_id, "created_at": created_at}},
            upsert=True, return_document=ReturnDocument.AFTER,
        )
        return _clean(result) or {**doc, "id": new_id, "created_at": created_at}

    def list_push_subscriptions(self, user_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._push_subscriptions.find({"user_id": str(user_id), "active": True}).sort("updated_at", -1).limit(max(1, min(int(limit), 50)))
        return [_clean(row) or {} for row in rows]

    def active_push_subscription_count(self, user_id: str) -> int:
        return int(self._push_subscriptions.count_documents({"user_id": str(user_id), "active": True}))

    def delete_push_subscription(self, subscription_id: str, user_id: str) -> bool:
        return self._push_subscriptions.delete_one({"id": str(subscription_id), "user_id": str(user_id)}).deleted_count == 1

    def delete_push_subscription_by_id(self, subscription_id: str) -> bool:
        return self._push_subscriptions.delete_one({"id": str(subscription_id)}).deleted_count == 1

    def claim_web_push_delivery(self, *, now: datetime, lease_until: datetime, claim_id: str) -> dict[str, Any] | None:
        from pymongo import ReturnDocument
        due_or_unleased = {"$and": [
            {"web_push_delivery.status": {"$in": ["pending", "retry"]}},
            {"$or": [
                {"web_push_delivery.next_attempt_at": {"$exists": False}},
                {"web_push_delivery.next_attempt_at": {"$lte": now}},
            ]},
        ]}
        expired_lease = {"$and": [
            {"web_push_delivery.status": "sending"},
            {"web_push_delivery.lease_until": {"$lte": now}},
        ]}
        return _clean(self._notifications.find_one_and_update(
            {"$or": [due_or_unleased, expired_lease]},
            {"$set": {"web_push_delivery.status": "sending", "web_push_delivery.claim_id": str(claim_id),
                      "web_push_delivery.lease_until": lease_until, "web_push_delivery.updated_at": now},
             "$inc": {"web_push_delivery.attempts": 1}},
            sort=[("created_at", 1)], return_document=ReturnDocument.AFTER,
        ))

    def record_web_push_endpoint_result(self, notification_id: str, claim_id: str, subscription_id: str,
                                        result: Mapping[str, Any], *, now: datetime) -> bool:
        safe_id = str(subscription_id)
        if not safe_id.replace("-", "").isalnum():
            raise ValueError("INVALID_PUSH_SUBSCRIPTION_ID")
        result_doc = {**dict(result), "updated_at": now}
        update = self._notifications.update_one(
            {"id": str(notification_id), "web_push_delivery.status": "sending", "web_push_delivery.claim_id": str(claim_id)},
            {"$set": {f"web_push_delivery.endpoint_results.{safe_id}": result_doc,
                      "web_push_delivery.updated_at": now}},
        )
        return bool(update.modified_count)

    def finish_web_push_delivery(self, notification_id: str, claim_id: str, *, outcome: str,
                                 now: datetime, next_attempt_at: datetime | None = None,
                                 error_code: str | None = None) -> bool:
        if outcome not in {"sent", "retry", "dead", "suppressed"}:
            raise ValueError("INVALID_WEB_PUSH_DELIVERY_OUTCOME")
        set_fields: dict[str, Any] = {"web_push_delivery.status": outcome, "web_push_delivery.updated_at": now}
        unset_fields: dict[str, str] = {"web_push_delivery.claim_id": "", "web_push_delivery.lease_until": ""}
        if outcome == "sent":
            set_fields["web_push_delivery.sent_at"] = now
            unset_fields["web_push_delivery.next_attempt_at"] = ""
            unset_fields["web_push_delivery.last_error_code"] = ""
        elif outcome == "retry":
            set_fields["web_push_delivery.next_attempt_at"] = next_attempt_at or now
            if error_code:
                set_fields["web_push_delivery.last_error_code"] = str(error_code)[:100]
        else:
            set_fields["web_push_delivery.finished_at"] = now
            unset_fields["web_push_delivery.next_attempt_at"] = ""
            if error_code:
                set_fields["web_push_delivery.last_error_code"] = str(error_code)[:100]
        result = self._notifications.update_one(
            {"id": str(notification_id), "web_push_delivery.status": "sending", "web_push_delivery.claim_id": str(claim_id)},
            {"$set": set_fields, "$unset": unset_fields},
        )
        return bool(result.modified_count)

    def list_notifications(self, user_id: str, *, unread_only: bool = False, limit: int = 50, before: datetime | None = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"user_id": str(user_id), "in_app": {"$ne": False}}
        if unread_only:
            query["read_at"] = None
        if before is not None:
            query["created_at"] = {"$lt": before}
        rows = self._notifications.find(query).sort("created_at", -1).limit(max(1, min(int(limit), 100)))
        return [_clean(row) or {} for row in rows]

    def unread_count(self, user_id: str) -> int:
        return int(self._notifications.count_documents({"user_id": str(user_id), "in_app": {"$ne": False}, "read_at": None}))

    def mark_notification_read(self, notification_id: str, user_id: str, *, now: datetime) -> dict[str, Any] | None:
        from pymongo import ReturnDocument
        return _clean(self._notifications.find_one_and_update(
            {"id": str(notification_id), "user_id": str(user_id), "in_app": {"$ne": False}},
            {"$set": {"read_at": now}}, return_document=ReturnDocument.AFTER,
        ))

    def mark_all_notifications_read(self, user_id: str, *, now: datetime) -> int:
        result = self._notifications.update_many(
            {"user_id": str(user_id), "in_app": {"$ne": False}, "read_at": None},
            {"$set": {"read_at": now}},
        )
        return int(result.modified_count)

    def delete_notification(self, notification_id: str, user_id: str) -> bool:
        row = self._notifications.find_one({"id": str(notification_id), "user_id": str(user_id)})
        if not row:
            return False
        delivery = row.get("telegram_delivery")
        if isinstance(delivery, Mapping) and delivery.get("status") in {"pending", "retry", "sending"}:
            result = self._notifications.update_one(
                {"id": str(notification_id), "user_id": str(user_id)},
                {"$set": {"in_app": False, "deleted_at": _now()}},
            )
            return bool(result.modified_count)
        return self._notifications.delete_one({"id": str(notification_id), "user_id": str(user_id)}).deleted_count == 1

    def health(self) -> dict[str, Any]:
        try:
            self._db.command("ping")
            return {"status": "ok", "adapter": "mongodb", "persistent": True}
        except Exception:
            return {"status": "error", "adapter": "mongodb", "persistent": True}

    def close(self) -> None:
        self._client.close()


def user_feature_repository_from_environment() -> UserFeatureRepository:
    environment = os.getenv("FTRADE_ENV", "development").strip().lower()
    if os.getenv("MONGODB_URI"):
        return MongoUserFeatureRepository.from_env()
    if environment == "production":
        raise RuntimeError("FTRADE_ENV=production requiere MONGODB_URI y DATABASE_NAME")
    return InMemoryUserFeatureRepository()
