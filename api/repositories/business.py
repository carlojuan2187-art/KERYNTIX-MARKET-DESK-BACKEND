"""Persistence for billing, referral rewards, support tickets, and admin audit.

Production uses MongoDB unique indexes/CAS updates. The in-memory adapter exists
only for deterministic tests and local development; it is not cross-process.
"""
from __future__ import annotations

import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _active_payment_status(status: str) -> bool:
    return status in {"pending", "verifying", "verified"}


class InMemoryBusinessRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.payments: dict[str, dict[str, Any]] = {}
        self.payment_txs: dict[str, dict[str, Any]] = {}
        self.referrals: dict[str, dict[str, Any]] = {}  # referred user id -> record
        self.referral_rewards: dict[str, dict[str, Any]] = {}
        self.tickets: dict[str, dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []
        self.settings: dict[str, dict[str, Any]] = {}

    def expire_payments(self, now: datetime) -> int:
        count = 0
        with self._lock:
            for doc in self.payments.values():
                status = str(doc.get("status"))
                expires_at = doc.get("expires_at")
                # Verified transfers do not expire while Premium activation is
                # pending. Only pending/verifying requests with a real expiry date
                # can be expired and have their reservation released.
                if status in {"pending", "verifying"} and isinstance(expires_at, datetime) and expires_at <= now:
                    doc.update({"status": "expired", "updated_at": now})
                    doc.pop("active_slot", None)
                    doc.pop("active_amount", None)
                    doc.pop("verification_lease_until", None)
                    count += 1
        return count

    def get_active_payment(self, user_id: str, now: datetime) -> dict[str, Any] | None:
        with self._lock:
            docs = [d for d in self.payments.values() if str(d.get("user_id")) == str(user_id) and ((str(d.get("status")) == "verified") or (_active_payment_status(str(d.get("status"))) and d.get("expires_at") > now))]
            docs.sort(key=lambda d: d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return deepcopy(docs[0]) if docs else None

    def active_amounts(self, now: datetime) -> set[int]:
        with self._lock:
            return {int(d["expected_units"]) for d in self.payments.values() if str(d.get("status")) == "verified" or (_active_payment_status(str(d.get("status"))) and d.get("expires_at") > now)}

    def create_payment(self, document: Mapping[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            user_id = str(document["user_id"])
            amount = int(document["expected_units"])
            if any(str(d.get("user_id")) == user_id and _active_payment_status(str(d.get("status"))) for d in self.payments.values()):
                return None
            if any(int(d.get("expected_units", -1)) == amount and _active_payment_status(str(d.get("status"))) for d in self.payments.values()):
                return None
            doc = deepcopy(dict(document))
            self.payments[str(doc["payment_id"])] = doc
            return deepcopy(doc)

    def list_all_payments(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            docs = [deepcopy(d) for d in self.payments.values() if status is None or str(d.get("status")) == status]
        docs.sort(key=lambda d: d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return docs[:max(1, min(int(limit), 200))]

    def list_payments_needing_activation(self, *, limit: int = 200) -> list[dict[str, Any]]:
        """Return verified transfers whose Premium activation has not completed."""
        with self._lock:
            docs = [deepcopy(d) for d in self.payments.values()
                    if str(d.get("status")) == "verified" and str(d.get("activation_status", "pending")) != "completed"]
        # Oldest retry/update time first so a repeatedly failing activation does not
        # monopolize the batch ahead of newer recoverable payments.
        docs.sort(key=lambda d: d.get("updated_at") or d.get("verified_at") or d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc))
        return docs[:max(1, min(int(limit), 500))]

    def list_pending_referral_reward_referrers(self, *, limit: int = 500) -> list[str]:
        with self._lock:
            pending = [d for d in self.referral_rewards.values()
                       if str(d.get("status")) == "pending" and d.get("referrer_id")]
        oldest_by_referrer: dict[str, datetime] = {}
        for doc in pending:
            referrer = str(doc.get("referrer_id"))
            stamp = doc.get("last_attempt_at") or doc.get("created_at") or datetime.min.replace(tzinfo=timezone.utc)
            oldest_by_referrer[referrer] = min(oldest_by_referrer.get(referrer, stamp), stamp)
        ordered = sorted(oldest_by_referrer, key=lambda referrer: (oldest_by_referrer[referrer], referrer))
        return ordered[:max(1, min(int(limit), 2000))]

    def get_payment(self, payment_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            doc = self.payments.get(str(payment_id))
            if not doc or (user_id is not None and str(doc.get("user_id")) != str(user_id)):
                return None
            return deepcopy(doc)

    def list_payments(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            docs = [deepcopy(d) for d in self.payments.values() if str(d.get("user_id")) == str(user_id)]
        docs.sort(key=lambda d: d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return docs[:max(1, min(int(limit), 100))]

    def claim_payment_verification(self, payment_id: str, user_id: str, *, now: datetime, lease_until: datetime, sender: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self.payments.get(str(payment_id))
            if not doc or str(doc.get("user_id")) != str(user_id):
                return None
            if doc.get("status") == "verified":
                return deepcopy(doc)
            lease = doc.get("verification_lease_until")
            if doc.get("status") == "verifying" and lease and lease > now:
                return None
            if doc.get("status") not in {"pending", "verifying"}:
                return None
            doc.update({"status": "verifying", "verification_lease_until": lease_until, "sender_address": sender, "last_attempt_at": now, "attempts": int(doc.get("attempts", 0)) + 1, "updated_at": now})
            return deepcopy(doc)

    def update_payment(self, payment_id: str, fields: Mapping[str, Any], *, unset: tuple[str, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            doc = self.payments.get(str(payment_id))
            if not doc:
                return None
            doc.update(deepcopy(dict(fields)))
            for key in unset:
                doc.pop(key, None)
            return deepcopy(doc)

    def claim_payment_tx(self, txid: str, document: Mapping[str, Any]) -> bool:
        with self._lock:
            prior = self.payment_txs.get(str(txid))
            if prior:
                return str(prior.get("payment_id")) == str(document.get("payment_id"))
            self.payment_txs[str(txid)] = deepcopy(dict(document))
            return True

    def get_payment_tx(self, txid: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self.payment_txs.get(str(txid))
            return deepcopy(doc) if doc else None

    def delete_payment_tx(self, txid: str) -> None:
        with self._lock:
            self.payment_txs.pop(str(txid), None)

    def record_referral(self, document: Mapping[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        with self._lock:
            referred_id = str(document["referred_id"])
            if referred_id in self.referrals:
                return deepcopy(self.referrals[referred_id]), False
            doc = deepcopy(dict(document))
            self.referrals[referred_id] = doc
            return deepcopy(doc), True

    def list_referrals(self, referrer_id: str) -> list[dict[str, Any]]:
        with self._lock:
            docs = [deepcopy(d) for d in self.referrals.values() if str(d.get("referrer_id")) == str(referrer_id)]
        docs.sort(key=lambda d: d.get("activated_at") or datetime.min.replace(tzinfo=timezone.utc))
        return docs

    def claim_referral_reward(self, referrer_id: str, *, now: datetime) -> dict[str, Any] | None:
        with self._lock:
            pending = sorted([d for d in self.referrals.values() if str(d.get("referrer_id")) == str(referrer_id) and d.get("activated_plan") == "premium" and not d.get("reward_applied") and not d.get("reward_claim_id")], key=lambda d: d.get("activated_at") or datetime.min.replace(tzinfo=timezone.utc))
            if len(pending) < 5:
                return None
            chosen = pending[:5]
            referral_ids = [str(d["referral_id"]) for d in chosen]
            reward_id = f"refreward:{referrer_id}:{'-'.join(referral_ids)}"
            existing = self.referral_rewards.get(reward_id)
            if existing:
                return deepcopy(existing)
            reward = {"reward_id": reward_id, "referrer_id": str(referrer_id), "referral_ids": referral_ids, "status": "pending", "created_at": now, "duration_days": 30}
            self.referral_rewards[reward_id] = reward
            for d in chosen:
                d["reward_claim_id"] = reward_id
            return deepcopy(reward)

    def list_referral_rewards(self, referrer_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            docs = [deepcopy(d) for d in self.referral_rewards.values() if str(d.get("referrer_id")) == str(referrer_id)]
        docs.sort(key=lambda d: d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return docs[:max(1, min(int(limit), 500))]

    def list_pending_referral_rewards(self, referrer_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [deepcopy(d) for d in self.referral_rewards.values() if str(d.get("referrer_id")) == str(referrer_id) and d.get("status") == "pending"]

    def finish_referral_reward(self, reward_id: str, *, applied: bool, now: datetime, error: str | None = None) -> bool:
        with self._lock:
            reward = self.referral_rewards.get(str(reward_id))
            if not reward:
                return False
            if applied:
                reward.update({"status": "applied", "applied_at": now, "error": None})
                for referral_id in reward.get("referral_ids", []):
                    for doc in self.referrals.values():
                        if str(doc.get("referral_id")) == str(referral_id):
                            doc["reward_applied"] = True
                            doc["rewarded_at"] = now
            else:
                reward.update({"status": "pending", "last_error": (error or "activation failed")[:300], "last_attempt_at": now})
                # Keep reward_claim_id to preserve the same deterministic reward batch.
            return True

    def create_ticket(self, document: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            doc = deepcopy(dict(document))
            self.tickets[str(doc["ticket_id"])] = doc
            return deepcopy(doc)

    def get_ticket(self, ticket_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            doc = self.tickets.get(str(ticket_id))
            if not doc or (user_id is not None and str(doc.get("user_id")) != str(user_id)):
                return None
            return deepcopy(doc)

    def list_tickets(self, *, user_id: str | None = None, status: str | None = None, limit: int = 50, before: datetime | None = None) -> list[dict[str, Any]]:
        with self._lock:
            docs = [deepcopy(d) for d in self.tickets.values() if (user_id is None or str(d.get("user_id")) == str(user_id)) and (status is None or d.get("status") == status) and (before is None or d.get("updated_at", d.get("created_at")) < before)]
        docs.sort(key=lambda d: d.get("updated_at", d.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return docs[:max(1, min(int(limit), 100))]

    def append_ticket_message(self, ticket_id: str, message: Mapping[str, Any], fields: Mapping[str, Any], *, user_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            doc = self.tickets.get(str(ticket_id))
            if not doc or (user_id is not None and str(doc.get("user_id")) != str(user_id)):
                return None
            doc.setdefault("messages", []).append(deepcopy(dict(message)))
            doc.update(deepcopy(dict(fields)))
            return deepcopy(doc)

    def update_ticket(self, ticket_id: str, fields: Mapping[str, Any], *, user_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            doc = self.tickets.get(str(ticket_id))
            if not doc or (user_id is not None and str(doc.get("user_id")) != str(user_id)):
                return None
            doc.update(deepcopy(dict(fields)))
            return deepcopy(doc)

    def write_audit(self, document: Mapping[str, Any]) -> None:
        with self._lock:
            self.audit.append(deepcopy(dict(document)))
            if len(self.audit) > 10000:
                del self.audit[:-10000]

    def list_audit(self, *, limit: int = 100, before: datetime | None = None) -> list[dict[str, Any]]:
        with self._lock:
            docs = [deepcopy(d) for d in self.audit if before is None or d.get("created_at") < before]
        docs.sort(key=lambda d: d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return docs[:max(1, min(int(limit), 200))]

    def get_setting(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self.settings.get(str(key))
            return deepcopy(value) if value is not None else None

    def set_setting(self, key: str, value: Mapping[str, Any] | None) -> None:
        with self._lock:
            if value is None:
                self.settings.pop(str(key), None)
            else:
                self.settings[str(key)] = deepcopy(dict(value))

    def admin_summary(self, now: datetime) -> dict[str, int]:
        with self._lock:
            payments = list(self.payments.values())
            tickets = list(self.tickets.values())
        return {"payments_total": len(payments), "payments_pending": sum(1 for d in payments if d.get("status") in {"pending", "verifying", "verified"}),
                "payments_completed": sum(1 for d in payments if d.get("status") == "completed"),
                "tickets_open": sum(1 for d in tickets if d.get("status") != "closed"), "tickets_total": len(tickets)}

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "adapter": "memory", "persistent": False, "payments": len(self.payments), "support_tickets": len(self.tickets)}

    def close(self) -> None:
        return None


class MongoBusinessRepository:
    def __init__(self, *, client: Any, db: Any, close_client: bool = False) -> None:
        self._client, self._db, self._close_client = client, db, close_client
        self._payments = db[os.getenv("PAYMENTS_COLLECTION", "payments")]
        self._payment_txs = db[os.getenv("PAYMENT_TXS_COLLECTION", "payment_txs")]
        self._referrals = db[os.getenv("REFERRALS_COLLECTION", "referrals")]
        self._referral_rewards = db[os.getenv("REFERRAL_REWARDS_COLLECTION", "referral_rewards")]
        self._tickets = db[os.getenv("SUPPORT_TICKETS_COLLECTION", "support_tickets")]
        self._audit = db[os.getenv("ADMIN_AUDIT_COLLECTION", "admin_audit")]
        self._settings = db[os.getenv("APP_SETTINGS_COLLECTION", "app_settings")]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self._payments.create_index("payment_id", unique=True, name="uniq_payment_id")
        self._payments.create_index("active_slot", unique=True, sparse=True, name="uniq_active_payment_user")
        self._payments.create_index("active_amount", unique=True, sparse=True, name="uniq_active_payment_amount")
        self._payments.create_index([("user_id", 1), ("created_at", -1)], name="payments_user_created")
        self._payment_txs.create_index("txid", unique=True, name="uniq_payment_txid")
        self._referrals.create_index("referred_id", unique=True, name="uniq_referral_referred")
        self._referrals.create_index([("referrer_id", 1), ("activated_at", 1)], name="referrals_referrer_created")
        self._referral_rewards.create_index("reward_id", unique=True, name="uniq_referral_reward_id")
        self._referral_rewards.create_index([("referrer_id", 1), ("status", 1)], name="referral_rewards_status")
        self._tickets.create_index("ticket_id", unique=True, name="uniq_support_ticket_id")
        self._tickets.create_index([("user_id", 1), ("updated_at", -1)], name="tickets_user_updated")
        self._audit.create_index("event_id", unique=True, name="uniq_admin_audit_event")
        self._audit.create_index([("created_at", -1)], name="admin_audit_created")

    @staticmethod
    def _clean(doc: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not doc:
            return None
        result = dict(doc)
        result.pop("_id", None)
        return result

    @classmethod
    def from_env(cls) -> "MongoBusinessRepository":
        uri = os.getenv("MONGODB_URI")
        db_name = os.getenv("DATABASE_NAME")
        if not uri or not db_name:
            raise RuntimeError("MONGODB_URI y DATABASE_NAME son obligatorios para operaciones de negocio persistentes")
        from pymongo import MongoClient
        timeout = max(1000, int(os.getenv("MONGODB_TIMEOUT_MS", "10000")))
        client = MongoClient(uri, serverSelectionTimeoutMS=timeout, connectTimeoutMS=timeout, socketTimeoutMS=timeout, retryWrites=True)
        return cls(client=client, db=client[db_name], close_client=True)

    def expire_payments(self, now: datetime) -> int:
        result = self._payments.update_many({"status": {"$in": ["pending", "verifying"]}, "expires_at": {"$lte": now}}, {"$set": {"status": "expired", "updated_at": now}, "$unset": {"active_slot": "", "active_amount": "", "verification_lease_until": ""}})
        return int(result.modified_count)

    def get_active_payment(self, user_id: str, now: datetime) -> dict[str, Any] | None:
        return self._clean(self._payments.find_one({"user_id": str(user_id), "$or": [{"status": "verified"}, {"status": {"$in": ["pending", "verifying"]}, "expires_at": {"$gt": now}}]}, sort=[("created_at", -1)]))

    def active_amounts(self, now: datetime) -> set[int]:
        return {int(doc["expected_units"]) for doc in self._payments.find({"$or": [{"status": "verified"}, {"status": {"$in": ["pending", "verifying"]}, "expires_at": {"$gt": now}}]}, {"expected_units": 1})}

    def create_payment(self, document: Mapping[str, Any]) -> dict[str, Any] | None:
        from pymongo.errors import DuplicateKeyError
        doc = dict(document)
        try:
            self._payments.insert_one(doc)
            return self._clean(doc)
        except DuplicateKeyError:
            return None

    def list_all_payments(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = {"status": status} if status else {}
        cursor = self._payments.find(query).sort("created_at", -1).limit(max(1, min(int(limit), 200)))
        return [self._clean(d) or {} for d in cursor]

    def list_payments_needing_activation(self, *, limit: int = 200) -> list[dict[str, Any]]:
        query = {"status": "verified", "activation_status": {"$ne": "completed"}}
        cursor = self._payments.find(query).sort([("updated_at", 1), ("verified_at", 1)]).limit(max(1, min(int(limit), 500)))
        return [self._clean(d) or {} for d in cursor]

    def list_pending_referral_reward_referrers(self, *, limit: int = 500) -> list[str]:
        cursor = self._referral_rewards.find({"status": "pending"}, {"referrer_id": 1}).sort([("last_attempt_at", 1), ("created_at", 1)])
        values: list[str] = []
        seen: set[str] = set()
        max_rows = max(1, min(int(limit), 2000))
        for document in cursor:
            value = str(document.get("referrer_id") or "")
            if value and value not in seen:
                seen.add(value)
                values.append(value)
                if len(values) >= max_rows:
                    break
        return values

    def get_payment(self, payment_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        query: dict[str, Any] = {"payment_id": str(payment_id)}
        if user_id is not None:
            query["user_id"] = str(user_id)
        return self._clean(self._payments.find_one(query))

    def list_payments(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        cursor = self._payments.find({"user_id": str(user_id)}).sort("created_at", -1).limit(max(1, min(int(limit), 100)))
        return [self._clean(d) or {} for d in cursor]

    def claim_payment_verification(self, payment_id: str, user_id: str, *, now: datetime, lease_until: datetime, sender: str) -> dict[str, Any] | None:
        from pymongo import ReturnDocument
        query = {"payment_id": str(payment_id), "user_id": str(user_id), "$or": [{"status": "pending"}, {"status": "verifying", "verification_lease_until": {"$lte": now}}]}
        doc = self._payments.find_one_and_update(query, {"$set": {"status": "verifying", "verification_lease_until": lease_until, "sender_address": sender, "last_attempt_at": now, "updated_at": now}, "$inc": {"attempts": 1}}, return_document=ReturnDocument.AFTER)
        return self._clean(doc)

    def update_payment(self, payment_id: str, fields: Mapping[str, Any], *, unset: tuple[str, ...] = ()) -> dict[str, Any] | None:
        update: dict[str, Any] = {"$set": dict(fields)}
        if unset:
            update["$unset"] = {key: "" for key in unset}
        from pymongo import ReturnDocument
        return self._clean(self._payments.find_one_and_update({"payment_id": str(payment_id)}, update, return_document=ReturnDocument.AFTER))

    def claim_payment_tx(self, txid: str, document: Mapping[str, Any]) -> bool:
        from pymongo.errors import DuplicateKeyError
        try:
            self._payment_txs.insert_one({**dict(document), "txid": str(txid)})
            return True
        except DuplicateKeyError:
            existing = self._payment_txs.find_one({"txid": str(txid)}, {"payment_id": 1})
            return bool(existing and str(existing.get("payment_id")) == str(document.get("payment_id")))

    def get_payment_tx(self, txid: str) -> dict[str, Any] | None:
        return self._clean(self._payment_txs.find_one({"txid": str(txid)}))

    def delete_payment_tx(self, txid: str) -> None:
        self._payment_txs.delete_one({"txid": str(txid)})

    def record_referral(self, document: Mapping[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        from pymongo.errors import DuplicateKeyError
        doc = dict(document)
        try:
            self._referrals.insert_one(doc)
            return self._clean(doc), True
        except DuplicateKeyError:
            return self._clean(self._referrals.find_one({"referred_id": str(doc["referred_id"])})), False

    def list_referrals(self, referrer_id: str) -> list[dict[str, Any]]:
        return [self._clean(d) or {} for d in self._referrals.find({"referrer_id": str(referrer_id)}).sort("activated_at", 1)]

    def claim_referral_reward(self, referrer_id: str, *, now: datetime) -> dict[str, Any] | None:
        from pymongo.errors import DuplicateKeyError
        pending = list(self._referrals.find({"referrer_id": str(referrer_id), "activated_plan": "premium", "reward_applied": {"$ne": True}, "reward_claim_id": {"$exists": False}}).sort("activated_at", 1).limit(5))
        if len(pending) < 5:
            return None
        referral_ids = [str(doc["referral_id"]) for doc in pending]
        reward_id = f"refreward:{referrer_id}:{'-'.join(referral_ids)}"
        reward = {"reward_id": reward_id, "referrer_id": str(referrer_id), "referral_ids": referral_ids, "status": "pending", "created_at": now, "duration_days": 30}
        try:
            self._referral_rewards.insert_one(reward)
        except DuplicateKeyError:
            existing = self._referral_rewards.find_one({"reward_id": reward_id})
            return self._clean(existing)
        # CAS-claim each selected referral. A partially claimed batch remains tied
        # to this deterministic reward id and will be reconciled by the same claim.
        for referral_id in referral_ids:
            self._referrals.update_one({"referral_id": referral_id, "reward_claim_id": {"$exists": False}, "reward_applied": {"$ne": True}}, {"$set": {"reward_claim_id": reward_id}})
        return self._clean(self._referral_rewards.find_one({"reward_id": reward_id}))

    def list_referral_rewards(self, referrer_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        cursor = self._referral_rewards.find({"referrer_id": str(referrer_id)}).sort("created_at", -1).limit(max(1, min(int(limit), 500)))
        return [self._clean(d) or {} for d in cursor]

    def list_pending_referral_rewards(self, referrer_id: str) -> list[dict[str, Any]]:
        return [self._clean(d) or {} for d in self._referral_rewards.find({"referrer_id": str(referrer_id), "status": "pending"}).sort("created_at", 1)]

    def finish_referral_reward(self, reward_id: str, *, applied: bool, now: datetime, error: str | None = None) -> bool:
        from pymongo import ReturnDocument
        fields: dict[str, Any] = {"status": "applied" if applied else "pending", "last_attempt_at": now, "last_error": None if applied else (error or "activation failed")[:300]}
        if applied:
            fields["applied_at"] = now
        updated = self._referral_rewards.find_one_and_update({"reward_id": str(reward_id), "status": "pending"}, {"$set": fields}, return_document=ReturnDocument.AFTER)
        if not updated:
            return self._referral_rewards.find_one({"reward_id": str(reward_id), "status": "applied"}) is not None
        if applied:
            self._referrals.update_many({"referral_id": {"$in": list(updated.get("referral_ids", []))}, "reward_claim_id": str(reward_id)}, {"$set": {"reward_applied": True, "rewarded_at": now}})
        return True

    def create_ticket(self, document: Mapping[str, Any]) -> dict[str, Any]:
        doc = dict(document)
        self._tickets.insert_one(doc)
        return self._clean(doc) or {}

    def get_ticket(self, ticket_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        query: dict[str, Any] = {"ticket_id": str(ticket_id)}
        if user_id is not None:
            query["user_id"] = str(user_id)
        return self._clean(self._tickets.find_one(query))

    def list_tickets(self, *, user_id: str | None = None, status: str | None = None, limit: int = 50, before: datetime | None = None) -> list[dict[str, Any]]:
        query: dict[str, Any] = {}
        if user_id is not None:
            query["user_id"] = str(user_id)
        if status is not None:
            query["status"] = status
        if before is not None:
            query["updated_at"] = {"$lt": before}
        cursor = self._tickets.find(query).sort("updated_at", -1).limit(max(1, min(int(limit), 100)))
        return [self._clean(d) or {} for d in cursor]

    def append_ticket_message(self, ticket_id: str, message: Mapping[str, Any], fields: Mapping[str, Any], *, user_id: str | None = None) -> dict[str, Any] | None:
        from pymongo import ReturnDocument
        query: dict[str, Any] = {"ticket_id": str(ticket_id)}
        if user_id is not None:
            query["user_id"] = str(user_id)
        return self._clean(self._tickets.find_one_and_update(query, {"$push": {"messages": dict(message)}, "$set": dict(fields)}, return_document=ReturnDocument.AFTER))

    def update_ticket(self, ticket_id: str, fields: Mapping[str, Any], *, user_id: str | None = None) -> dict[str, Any] | None:
        from pymongo import ReturnDocument
        query: dict[str, Any] = {"ticket_id": str(ticket_id)}
        if user_id is not None:
            query["user_id"] = str(user_id)
        return self._clean(self._tickets.find_one_and_update(query, {"$set": dict(fields)}, return_document=ReturnDocument.AFTER))

    def write_audit(self, document: Mapping[str, Any]) -> None:
        self._audit.insert_one(dict(document))

    def list_audit(self, *, limit: int = 100, before: datetime | None = None) -> list[dict[str, Any]]:
        query = {"created_at": {"$lt": before}} if before is not None else {}
        cursor = self._audit.find(query).sort("created_at", -1).limit(max(1, min(int(limit), 200)))
        return [self._clean(d) or {} for d in cursor]

    def get_setting(self, key: str) -> dict[str, Any] | None:
        doc = self._settings.find_one({"_id": str(key)})
        if not doc:
            return None
        result = dict(doc)
        result.pop("_id", None)
        return result

    def set_setting(self, key: str, value: Mapping[str, Any] | None) -> None:
        if value is None:
            self._settings.delete_one({"_id": str(key)})
        else:
            self._settings.replace_one({"_id": str(key)}, dict(value), upsert=True)

    def admin_summary(self, now: datetime) -> dict[str, int]:
        pending = {"status": {"$in": ["pending", "verifying", "verified"]}}
        return {"payments_total": int(self._payments.count_documents({})),
                "payments_pending": int(self._payments.count_documents(pending)),
                "payments_completed": int(self._payments.count_documents({"status": "completed"})),
                "tickets_open": int(self._tickets.count_documents({"status": {"$ne": "closed"}})),
                "tickets_total": int(self._tickets.count_documents({}))}

    def health(self) -> dict[str, Any]:
        try:
            self._db.command("ping")
            return {"status": "ok", "adapter": "mongodb", "persistent": True}
        except Exception as exc:
            return {"status": "error", "adapter": "mongodb", "persistent": True, "error": type(exc).__name__}

    def close(self) -> None:
        if self._close_client:
            self._client.close()


def business_repository_from_environment(auth_repository: Any | None = None) -> InMemoryBusinessRepository | MongoBusinessRepository:
    # Reuse the same MongoClient/database as auth when the real repository is injected.
    if auth_repository is not None and hasattr(auth_repository, "_db") and hasattr(auth_repository, "_client"):
        return MongoBusinessRepository(client=auth_repository._client, db=auth_repository._db, close_client=False)
    if auth_repository is not None and auth_repository.__class__.__name__ == "InMemoryAuthRepository":
        return InMemoryBusinessRepository()
    return MongoBusinessRepository.from_env()
