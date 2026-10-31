"""Persistence adapters for phone challenges, users, and refresh sessions.

The in-memory implementation is for deterministic tests and local development only.
Production must configure MongoDB; it never silently falls back to memory.
"""
from __future__ import annotations

from api.mongo_config import mongo_pool_options

import os
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    return None


class AuthRepository(Protocol):
    def create_auth_request(self, document: Mapping[str, Any]) -> None: ...
    def get_auth_request(self, token_hash: str) -> dict[str, Any] | None: ...
    def get_latest_login_request_for_phone(self, phone: str) -> dict[str, Any] | None: ...
    def get_pending_request_for_chat(self, chat_id: str) -> dict[str, Any] | None: ...
    def update_auth_request(self, token_hash: str, fields: Mapping[str, Any], *, conditions: Mapping[str, Any] | None = None) -> dict[str, Any] | None: ...
    def increment_auth_attempts(self, token_hash: str, *, now: datetime, field: str = "attempts", maximum: int = 5) -> dict[str, Any] | None: ...
    def find_recent_phone_request(self, phone: str, *, since: datetime) -> dict[str, Any] | None: ...
    def count_recent_auth_requests_by_ip(self, ip_hash: str, *, since: datetime) -> int: ...
    def count_recent_password_attempts(self, *, phone: str | None = None, ip_hash: str | None = None, since: datetime) -> int: ...
    def get_user_by_phone(self, phone: str) -> dict[str, Any] | None: ...
    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None: ...
    def get_user_by_telegram_chat_id(self, chat_id: str) -> dict[str, Any] | None: ...
    def get_user_by_ref_code(self, ref_code: str) -> dict[str, Any] | None: ...
    def count_referred_users(self, referrer_id: str) -> int: ...
    def admin_user_summary(self, *, now: datetime) -> dict[str, int]: ...
    def list_users(self, *, query: str | None = None, plan: str | None = None, banned: bool | None = None, skip: int = 0, limit: int = 50) -> tuple[list[dict[str, Any]], int]: ...
    def list_active_trial_users(self, *, query: str | None = None, skip: int = 0, limit: int = 50, now: datetime | None = None) -> tuple[list[dict[str, Any]], int]: ...
    def create_user_if_absent(self, user: Mapping[str, Any]) -> tuple[dict[str, Any], bool]: ...
    def update_user(self, user_id: str, fields: Mapping[str, Any]) -> dict[str, Any] | None: ...
    def add_watchlist_symbol(self, user_id: str, symbol: str, *, max_items: int = 10) -> tuple[dict[str, Any] | None, str]: ...
    def remove_watchlist_symbol(self, user_id: str, symbol: str) -> tuple[dict[str, Any] | None, bool]: ...
    def update_alert_preferences(self, user_id: str, fields: Mapping[str, bool]) -> dict[str, Any] | None: ...
    def list_users_for_alerts(self, *, limit: int = 500, after_user_id: str | None = None) -> list[dict[str, Any]]: ...
    def extend_premium_plan(self, user_id: str, days: int, event: Mapping[str, Any], *, now: datetime) -> tuple[dict[str, Any] | None, bool]: ...
    def list_users_for_plan_maintenance(self, *, since: datetime, until: datetime, limit: int = 1000) -> list[dict[str, Any]]: ...
    def claim_plan_notice(self, user_id: str, kind: str, boundary: datetime, *, now: datetime, lease_until: datetime) -> tuple[str, str] | None: ...
    def finish_plan_notice(self, dedupe_key: str, claim_id: str, *, sent: bool, now: datetime, error: str | None = None) -> bool: ...
    def create_session(self, session: Mapping[str, Any]) -> None: ...
    def get_session_by_refresh_hash(self, refresh_hash: str, *, grace_seconds: int = 0) -> dict[str, Any] | None: ...
    def rotate_session(self, session_id: str, old_hash: str, new_hash: str, *, now: datetime, expires_at: datetime, grace_seconds: int = 0) -> dict[str, Any] | None: ...
    def revoke_session_by_refresh_hash(self, refresh_hash: str, *, now: datetime) -> bool: ...
    def revoke_session(self, session_id: str, user_id: str, *, now: datetime) -> bool: ...
    def is_session_active(self, session_id: str, user_id: str, *, now: datetime) -> bool: ...
    def list_active_sessions(self, user_id: str, *, now: datetime) -> list[dict[str, Any]]: ...
    def health(self) -> dict[str, Any]: ...
    def close(self) -> None: ...


class InMemoryAuthRepository:
    """Thread-safe, non-persistent adapter for tests/local development."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._auth_requests: dict[str, dict[str, Any]] = {}
        self._users: dict[str, dict[str, Any]] = {}
        self._sessions: dict[str, dict[str, Any]] = {}
        self._plan_notices: dict[str, dict[str, Any]] = {}

    def create_auth_request(self, document: Mapping[str, Any]) -> None:
        with self._lock:
            key = str(document["token_hash"])
            if key in self._auth_requests:
                raise ValueError("AUTH_TOKEN_COLLISION")
            self._auth_requests[key] = deepcopy(dict(document))

    def get_auth_request(self, token_hash: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self._auth_requests.get(token_hash)
            return deepcopy(doc) if doc else None

    def get_latest_login_request_for_phone(self, phone: str) -> dict[str, Any] | None:
        with self._lock:
            found = [d for d in self._auth_requests.values() if d.get("flow") in {"login", "register"} and d.get("phone") == phone]
            found.sort(key=lambda d: _aware(d.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return deepcopy(found[0]) if found else None

    def get_pending_request_for_chat(self, chat_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = [d for d in self._auth_requests.values() if str(d.get("pending_chat_id") or "") == str(chat_id) and not d.get("contact_verified")]
            found.sort(key=lambda d: _aware(d.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            return deepcopy(found[0]) if found else None

    def update_auth_request(self, token_hash: str, fields: Mapping[str, Any], *, conditions: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        with self._lock:
            doc = self._auth_requests.get(token_hash)
            if doc is None:
                return None
            for key, expected in (conditions or {}).items():
                actual = doc.get(key)
                if key.endswith("_gt"):
                    actual = doc.get(key[:-3])
                    if not (_aware(actual) and _aware(actual) > _aware(expected)):
                        return None
                elif key.endswith("_lt"):
                    actual = doc.get(key[:-3])
                    if not (_aware(actual) and _aware(actual) < _aware(expected)):
                        return None
                elif actual != expected:
                    return None
            doc.update(deepcopy(dict(fields)))
            self._auth_requests[token_hash] = doc
            return deepcopy(doc)

    def increment_auth_attempts(self, token_hash: str, *, now: datetime, field: str = "attempts", maximum: int = 5) -> dict[str, Any] | None:
        with self._lock:
            doc = self._auth_requests.get(token_hash)
            if not doc or int(doc.get(field, 0)) >= maximum:
                return None
            expiry_field = "code_expires_at" if field == "attempts" else "link_expires_at"
            expiry = _aware(doc.get(expiry_field))
            if expiry and expiry <= now:
                return None
            doc[field] = int(doc.get(field, 0)) + 1
            self._auth_requests[token_hash] = doc
            return deepcopy(doc)

    def find_recent_phone_request(self, phone: str, *, since: datetime) -> dict[str, Any] | None:
        with self._lock:
            found = [d for d in self._auth_requests.values() if d.get("flow") in {"login", "register"} and d.get("phone") == phone and _aware(d.get("created_at")) and _aware(d.get("created_at")) >= since]
            found.sort(key=lambda d: _aware(d["created_at"]), reverse=True)
            return deepcopy(found[0]) if found else None

    def count_recent_auth_requests_by_ip(self, ip_hash: str, *, since: datetime) -> int:
        with self._lock:
            return sum(1 for d in self._auth_requests.values() if d.get("flow") in {"login", "register"} and d.get("requester_ip_hash") == ip_hash and _aware(d.get("created_at")) and _aware(d.get("created_at")) >= since)

    def count_recent_password_attempts(self, *, phone: str | None = None, ip_hash: str | None = None, since: datetime) -> int:
        with self._lock:
            return sum(
                1 for d in self._auth_requests.values()
                if d.get("flow") == "password_login"
                and (phone is None or d.get("phone") == phone)
                and (ip_hash is None or d.get("requester_ip_hash") == ip_hash)
                and _aware(d.get("created_at")) and _aware(d.get("created_at")) >= since
            )

    def get_user_by_phone(self, phone: str) -> dict[str, Any] | None:
        with self._lock:
            for doc in self._users.values():
                if doc.get("phone") == phone:
                    return deepcopy(doc)
        return None

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self._users.get(str(user_id))
            return deepcopy(doc) if doc else None

    def get_user_by_telegram_chat_id(self, chat_id: str) -> dict[str, Any] | None:
        with self._lock:
            for doc in self._users.values():
                if str(doc.get("telegram_chat_id") or "") == str(chat_id):
                    return deepcopy(doc)
        return None

    def get_user_by_ref_code(self, ref_code: str) -> dict[str, Any] | None:
        with self._lock:
            for doc in self._users.values():
                if doc.get("ref_code") == ref_code:
                    return deepcopy(doc)
        return None

    def count_referred_users(self, referrer_id: str) -> int:
        with self._lock:
            return sum(1 for user in self._users.values() if str(user.get("referred_by") or "") == str(referrer_id))

    def admin_user_summary(self, *, now: datetime) -> dict[str, int]:
        with self._lock:
            users = [deepcopy(d) for d in self._users.values()]
        premium = 0
        trial = 0
        active_bans = 0
        for user in users:
            plan_end = _aware(user.get("plan_end"))
            trial_end = _aware(user.get("trial_end"))
            ban_until = _aware(user.get("ban_until"))
            premium_active = str(user.get("plan", "free")).lower() == "premium" and plan_end is not None and plan_end > now
            trial_active = trial_end is not None and trial_end > now
            premium += int(premium_active)
            trial += int(not premium_active and trial_active)
            active_bans += int(bool(user.get("banned")) and (ban_until is None or ban_until > now))
        return {"users_total": len(users), "premium_active": premium, "trial_active": trial,
                "access_expired": max(0, len(users) - premium - trial), "bans_active": active_bans}

    def list_users(self, *, query: str | None = None, plan: str | None = None, banned: bool | None = None, skip: int = 0, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
        needle = (query or "").strip().lower()
        with self._lock:
            docs = [deepcopy(d) for d in self._users.values()]
        if needle:
            docs = [d for d in docs if needle in str(d.get("user_id", "")).lower() or needle in str(d.get("phone", "")).lower() or needle in str(d.get("username", "")).lower() or needle in str(d.get("ref_code", "")).lower()]
        if plan:
            docs = [d for d in docs if str(d.get("plan", "free")).lower() == plan.lower()]
        if banned is not None:
            now = datetime.now(timezone.utc)
            def is_active_ban(doc: Mapping[str, Any]) -> bool:
                if not bool(doc.get("banned", False)):
                    return False
                until = _aware(doc.get("ban_until"))
                return until is None or until > now
            docs = [d for d in docs if is_active_ban(d) == banned]
        docs.sort(key=lambda d: d.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        total = len(docs)
        page = docs[max(0, int(skip)):max(0, int(skip)) + max(1, min(int(limit), 100))]
        return page, total

    def list_active_trial_users(self, *, query: str | None = None, skip: int = 0, limit: int = 50, now: datetime | None = None) -> tuple[list[dict[str, Any]], int]:
        current = _aware(now) or _now_utc()
        needle = (query or "").strip().lower()
        with self._lock:
            docs = [deepcopy(d) for d in self._users.values()]
        if needle:
            docs = [d for d in docs if any(needle in str(d.get(field, "")).lower() for field in ("user_id", "phone", "username", "ref_code"))]
        active_trials = []
        for doc in docs:
            trial_end = _aware(doc.get("trial_end"))
            plan_end = _aware(doc.get("plan_end"))
            ban_until = _aware(doc.get("ban_until"))
            premium_active = str(doc.get("plan", "free")).lower() == "premium" and plan_end is not None and plan_end > current
            ban_active = bool(doc.get("banned", False)) and (ban_until is None or ban_until > current)
            if trial_end is not None and trial_end > current and not premium_active and not ban_active:
                active_trials.append(doc)
        active_trials.sort(key=lambda d: _aware(d.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        total = len(active_trials)
        start = max(0, int(skip))
        page = active_trials[start:start + max(1, min(int(limit), 100))]
        return page, total

    def create_user_if_absent(self, user: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        with self._lock:
            phone = str(user["phone"])
            existing = next((d for d in self._users.values() if d.get("phone") == phone), None)
            if existing:
                return deepcopy(existing), False
            chat_id = str(user.get("telegram_chat_id") or "")
            if chat_id and any(str(d.get("telegram_chat_id") or "") == chat_id for d in self._users.values()):
                raise ValueError("TELEGRAM_ALREADY_LINKED")
            doc = deepcopy(dict(user))
            self._users[str(doc["user_id"])] = doc
            return deepcopy(doc), True

    def update_user(self, user_id: str, fields: Mapping[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            doc = self._users.get(str(user_id))
            if doc is None:
                return None
            if "telegram_chat_id" in fields and fields["telegram_chat_id"]:
                owner = next((u for uid, u in self._users.items() if uid != str(user_id) and str(u.get("telegram_chat_id") or "") == str(fields["telegram_chat_id"])), None)
                if owner:
                    raise ValueError("TELEGRAM_ALREADY_LINKED")
            doc.update(deepcopy(dict(fields)))
            self._users[str(user_id)] = doc
            return deepcopy(doc)

    def add_watchlist_symbol(self, user_id: str, symbol: str, *, max_items: int = 10) -> tuple[dict[str, Any] | None, str]:
        with self._lock:
            user = self._users.get(str(user_id))
            if user is None:
                return None, "USER_NOT_FOUND"
            symbols = list(user.get("watchlist") or [])
            if symbol in symbols:
                return deepcopy(user), "DUPLICATE"
            if len(symbols) >= max(1, int(max_items)):
                return deepcopy(user), "FULL"
            symbols.append(symbol)
            user["watchlist"] = symbols
            user["watchlist_updated_at"] = _now_utc()
            return deepcopy(user), "ADDED"

    def remove_watchlist_symbol(self, user_id: str, symbol: str) -> tuple[dict[str, Any] | None, bool]:
        with self._lock:
            user = self._users.get(str(user_id))
            if user is None:
                return None, False
            symbols = list(user.get("watchlist") or [])
            if symbol not in symbols:
                return deepcopy(user), False
            user["watchlist"] = [item for item in symbols if item != symbol]
            user["watchlist_updated_at"] = _now_utc()
            return deepcopy(user), True

    def update_alert_preferences(self, user_id: str, fields: Mapping[str, bool]) -> dict[str, Any] | None:
        with self._lock:
            user = self._users.get(str(user_id))
            if user is None:
                return None
            current = user.get("alert_prefs") if isinstance(user.get("alert_prefs"), dict) else {}
            merged = dict(current)
            merged.update({str(key): bool(value) for key, value in fields.items()})
            user["alert_prefs"] = merged
            user["alert_prefs_updated_at"] = _now_utc()
            return deepcopy(user)

    def list_users_for_alerts(self, *, limit: int = 500, after_user_id: str | None = None) -> list[dict[str, Any]]:
        """Read alert recipients in stable, indexed user-id pages."""
        page_size = max(1, min(int(limit), 2000))
        with self._lock:
            ordered = sorted(
                (user for user in self._users.values()
                 if not after_user_id or str(user.get("user_id") or "") > str(after_user_id)),
                key=lambda user: str(user.get("user_id") or ""),
            )
            return [deepcopy({key: user.get(key) for key in (
                "user_id", "telegram_chat_id", "alert_prefs", "plan", "plan_end",
                "trial_end", "banned", "ban_until", "is_admin", "watchlist", "language",
            )}) for user in ordered[:page_size]]

    def create_session(self, session: Mapping[str, Any]) -> None:
        with self._lock:
            sid = str(session["id"])
            self._sessions[sid] = deepcopy(dict(session))

    @staticmethod
    def _matches_refresh(doc: Mapping[str, Any], refresh_hash: str, grace_seconds: int, now: datetime) -> bool:
        if doc.get("refresh_hash") == refresh_hash:
            return True
        prev, rotated_at = doc.get("prev_refresh_hash"), doc.get("rotated_at")
        return bool(grace_seconds > 0 and prev and prev == refresh_hash and rotated_at
                    and (now - _aware(rotated_at)).total_seconds() <= grace_seconds)

    def get_session_by_refresh_hash(self, refresh_hash: str, *, grace_seconds: int = 0) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            for doc in self._sessions.values():
                if (not doc.get("revoked_at") and _aware(doc.get("expires_at")) > now
                        and self._matches_refresh(doc, refresh_hash, grace_seconds, now)):
                    return deepcopy(doc)
        return None

    def rotate_session(self, session_id: str, old_hash: str, new_hash: str, *, now: datetime, expires_at: datetime, grace_seconds: int = 0) -> dict[str, Any] | None:
        with self._lock:
            doc = self._sessions.get(str(session_id))
            if not doc or doc.get("revoked_at") or _aware(doc.get("expires_at")) <= now or not self._matches_refresh(doc, old_hash, grace_seconds, now):
                return None
            doc.update({"prev_refresh_hash": doc.get("refresh_hash"), "rotated_at": now, "refresh_hash": new_hash, "expires_at": expires_at, "last_activity": now})
            return deepcopy(doc)

    def revoke_session_by_refresh_hash(self, refresh_hash: str, *, now: datetime) -> bool:
        with self._lock:
            for doc in self._sessions.values():
                if refresh_hash in (doc.get("refresh_hash"), doc.get("prev_refresh_hash")) and not doc.get("revoked_at"):
                    doc.update({"revoked_at": now, "refresh_hash": None, "prev_refresh_hash": None, "last_activity": now})
                    return True
        return False

    def revoke_session(self, session_id: str, user_id: str, *, now: datetime) -> bool:
        with self._lock:
            doc = self._sessions.get(str(session_id))
            if not doc or str(doc.get("user_id")) != str(user_id) or doc.get("revoked_at"):
                return False
            doc.update({"revoked_at": now, "refresh_hash": None, "prev_refresh_hash": None, "last_activity": now})
            return True

    def is_session_active(self, session_id: str, user_id: str, *, now: datetime) -> bool:
        with self._lock:
            doc = self._sessions.get(str(session_id))
            return bool(doc and str(doc.get("user_id")) == str(user_id) and not doc.get("revoked_at") and _aware(doc.get("expires_at")) and _aware(doc["expires_at"]) > now)

    def list_active_sessions(self, user_id: str, *, now: datetime) -> list[dict[str, Any]]:
        with self._lock:
            found = [deepcopy(d) for d in self._sessions.values() if str(d.get("user_id")) == str(user_id) and not d.get("revoked_at") and _aware(d.get("expires_at")) and _aware(d["expires_at"]) > now]
            return sorted(found, key=lambda d: _aware(d.get("last_activity")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    def extend_premium_plan(self, user_id: str, days: int, event: Mapping[str, Any], *, now: datetime) -> tuple[dict[str, Any] | None, bool]:
        with self._lock:
            user = self._users.get(str(user_id))
            if not user:
                return None, False
            event_id = str(event["event_id"])
            processed = list(user.get("processed_plan_event_ids") or [])
            if event_id in processed:
                return deepcopy(user), False
            current_end = _aware(user.get("plan_end"))
            start = current_end if str(user.get("plan", "free")).lower() == "premium" and current_end and current_end > now else now
            end = start + timedelta(days=int(days))
            record = deepcopy(dict(event))
            record.update({"effective_start": start, "effective_end": end})
            history = list(user.get("plan_history") or []) + [record]
            processed.append(event_id)
            user.update({
                "plan": "premium", "plan_end": end, "plan_updated_at": now,
                "plan_history": history[-100:], "processed_plan_event_ids": processed,
            })
            self._users[str(user_id)] = user
            return deepcopy(user), True

    def list_users_for_plan_maintenance(self, *, since: datetime, until: datetime, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            def in_window(value: Any) -> bool:
                value = _aware(value)
                return bool(value and since <= value <= until)
            found = [deepcopy(user) for user in self._users.values() if in_window(user.get("trial_end")) or (str(user.get("plan", "free")).lower() == "premium" and in_window(user.get("plan_end")))]
            found.sort(key=lambda user: min((dt for dt in (_aware(user.get("trial_end")), _aware(user.get("plan_end"))) if dt is not None), default=datetime.max.replace(tzinfo=timezone.utc)))
            return found[:max(1, min(int(limit), 5000))]

    def claim_plan_notice(self, user_id: str, kind: str, boundary: datetime, *, now: datetime, lease_until: datetime) -> tuple[str, str] | None:
        import uuid
        boundary_utc = _aware(boundary)
        dedupe_key = f"{user_id}:{kind}:{boundary_utc.isoformat() if boundary_utc else str(boundary)}"
        claim_id = uuid.uuid4().hex
        with self._lock:
            current = self._plan_notices.get(dedupe_key)
            if current and current.get("status") == "sent":
                return None
            if current and _aware(current.get("lease_until")) and _aware(current["lease_until"]) > now:
                return None
            self._plan_notices[dedupe_key] = {
                "dedupe_key": dedupe_key, "user_id": str(user_id), "kind": str(kind),
                "boundary": boundary_utc, "status": "claimed", "claim_id": claim_id,
                "lease_until": lease_until, "updated_at": now,
            }
            return dedupe_key, claim_id

    def finish_plan_notice(self, dedupe_key: str, claim_id: str, *, sent: bool, now: datetime, error: str | None = None) -> bool:
        with self._lock:
            notice = self._plan_notices.get(dedupe_key)
            if not notice or notice.get("claim_id") != claim_id or notice.get("status") != "claimed":
                return False
            notice.update({"status": "sent" if sent else "pending", "updated_at": now, "lease_until": now})
            if sent:
                notice["sent_at"] = now
                notice.pop("last_error", None)
            elif error:
                notice["last_error"] = str(error)[:200]
            self._plan_notices[dedupe_key] = notice
            return True

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "adapter": "memory", "persistent": False}

    def close(self) -> None:
        return None


class MongoAuthRepository:
    def __init__(self, *, client: Any, db: Any) -> None:
        self._client = client
        self._db = db
        self._users = db[os.getenv("USERS_COLLECTION", "users")]
        self._auth_requests = db[os.getenv("AUTH_REQUESTS_COLLECTION", "auth_requests")]
        self._sessions = db[os.getenv("SESSIONS_COLLECTION", "sessions")]
        self._plan_notifications = db[os.getenv("PLAN_NOTIFICATIONS_COLLECTION", "plan_notifications")]
        self._ensure_indexes()

    @classmethod
    def from_env(cls) -> "MongoAuthRepository":
        uri = os.getenv("MONGODB_URI")
        db_name = os.getenv("DATABASE_NAME")
        if not uri or not db_name:
            raise RuntimeError("MONGODB_URI y DATABASE_NAME son obligatorios para autenticación persistente")
        try:
            from pymongo import MongoClient
        except ImportError as exc:
            raise RuntimeError("Instala pymongo con: pip install 'pymongo>=4.7'") from exc
        timeout = max(1000, int(os.getenv("MONGODB_TIMEOUT_MS", "10000")))
        client = MongoClient(uri, serverSelectionTimeoutMS=timeout, connectTimeoutMS=timeout, socketTimeoutMS=timeout, retryWrites=True, **mongo_pool_options())
        return cls(client=client, db=client[db_name])

    def _ensure_indexes(self) -> None:
        self._users.create_index("user_id", name="users_alerts_pagination")
        self._users.create_index("phone", unique=True, partialFilterExpression={"phone": {"$type": "string"}}, name="uniq_user_phone")
        self._users.create_index("telegram_chat_id", unique=True, partialFilterExpression={"telegram_chat_id": {"$type": "string"}}, name="uniq_user_telegram_chat_id")
        self._users.create_index("ref_code", unique=True, partialFilterExpression={"ref_code": {"$type": "string"}}, name="uniq_user_ref_code")
        self._auth_requests.create_index("token_hash", unique=True, name="uniq_auth_token_hash")
        self._auth_requests.create_index([("phone", 1), ("created_at", -1)], name="auth_phone_created")
        self._auth_requests.create_index("pending_chat_id", name="auth_pending_chat")
        self._auth_requests.create_index([("requester_ip_hash", 1), ("created_at", -1)], name="auth_ip_created")
        self._auth_requests.create_index("created_at", expireAfterSeconds=86400, name="auth_requests_ttl")
        self._sessions.create_index("refresh_hash", unique=True, sparse=True, name="uniq_refresh_hash")
        self._sessions.create_index([("user_id", 1), ("expires_at", -1)], name="sessions_user_expires")
        self._sessions.create_index("expires_at", expireAfterSeconds=0, name="sessions_ttl")
        self._plan_notifications.create_index("dedupe_key", unique=True, name="uniq_plan_notice_dedupe")
        self._plan_notifications.create_index([("status", 1), ("lease_until", 1)], name="plan_notice_claims")

    @staticmethod
    def _clean(doc: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not doc:
            return None
        result = dict(doc)
        result.pop("_id", None)
        return result

    def create_auth_request(self, document: Mapping[str, Any]) -> None:
        self._auth_requests.insert_one(dict(document))

    def get_auth_request(self, token_hash: str) -> dict[str, Any] | None:
        return self._clean(self._auth_requests.find_one({"token_hash": token_hash}))

    def get_latest_login_request_for_phone(self, phone: str) -> dict[str, Any] | None:
        return self._clean(self._auth_requests.find_one({"phone": phone, "flow": {"$in": ["login", "register"]}}, sort=[("created_at", -1)]))

    def get_pending_request_for_chat(self, chat_id: str) -> dict[str, Any] | None:
        now = _now_utc()
        return self._clean(self._auth_requests.find_one({"pending_chat_id": str(chat_id), "contact_verified": {"$ne": True}, "link_expires_at": {"$gt": now}}, sort=[("created_at", -1)]))

    def update_auth_request(self, token_hash: str, fields: Mapping[str, Any], *, conditions: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        query: dict[str, Any] = {"token_hash": token_hash}
        for key, val in (conditions or {}).items():
            if key.endswith("_gt"):
                query[key[:-3]] = {"$gt": val}
            elif key.endswith("_lt"):
                query[key[:-3]] = {"$lt": val}
            else:
                query[key] = val
        try:
            from pymongo import ReturnDocument
            doc = self._auth_requests.find_one_and_update(query, {"$set": dict(fields)}, return_document=ReturnDocument.AFTER)
            return self._clean(doc)
        except Exception:
            raise

    def increment_auth_attempts(self, token_hash: str, *, now: datetime, field: str = "attempts", maximum: int = 5) -> dict[str, Any] | None:
        expiry_field = "code_expires_at" if field == "attempts" else "link_expires_at"
        from pymongo import ReturnDocument
        query = {"token_hash": token_hash, field: {"$lt": maximum}, expiry_field: {"$gt": now}}
        doc = self._auth_requests.find_one_and_update(query, {"$inc": {field: 1}}, return_document=ReturnDocument.AFTER)
        return self._clean(doc)

    def find_recent_phone_request(self, phone: str, *, since: datetime) -> dict[str, Any] | None:
        return self._clean(self._auth_requests.find_one({"phone": phone, "flow": {"$in": ["login", "register"]}, "created_at": {"$gte": since}}, sort=[("created_at", -1)]))

    def count_recent_auth_requests_by_ip(self, ip_hash: str, *, since: datetime) -> int:
        return int(self._auth_requests.count_documents({"flow": {"$in": ["login", "register"]}, "requester_ip_hash": ip_hash, "created_at": {"$gte": since}}))

    def count_recent_password_attempts(self, *, phone: str | None = None, ip_hash: str | None = None, since: datetime) -> int:
        query: dict[str, Any] = {"flow": "password_login", "created_at": {"$gte": since}}
        if phone is not None:
            query["phone"] = phone
        if ip_hash is not None:
            query["requester_ip_hash"] = ip_hash
        return int(self._auth_requests.count_documents(query))

    def get_user_by_phone(self, phone: str) -> dict[str, Any] | None:
        return self._clean(self._users.find_one({"phone": phone}))

    def get_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self._clean(self._users.find_one({"user_id": str(user_id)}))

    def get_user_by_telegram_chat_id(self, chat_id: str) -> dict[str, Any] | None:
        return self._clean(self._users.find_one({"telegram_chat_id": str(chat_id)}))

    def get_user_by_ref_code(self, ref_code: str) -> dict[str, Any] | None:
        return self._clean(self._users.find_one({"ref_code": ref_code}))

    def admin_user_summary(self, *, now: datetime) -> dict[str, int]:
        total = int(self._users.count_documents({}))
        premium = int(self._users.count_documents({"plan": "premium", "plan_end": {"$gt": now}}))
        trial = int(self._users.count_documents({"trial_end": {"$gt": now}, "$or": [{"plan": {"$ne": "premium"}}, {"plan_end": {"$lte": now}}, {"plan_end": None}]}))
        bans = int(self._users.count_documents({"banned": True, "$or": [{"ban_until": None}, {"ban_until": {"$gt": now}}]}))
        return {"users_total": total, "premium_active": premium, "trial_active": trial,
                "access_expired": max(0, total - premium - trial), "bans_active": bans}

    def count_referred_users(self, referrer_id: str) -> int:
        return int(self._users.count_documents({"referred_by": str(referrer_id)}))

    def list_users(self, *, query: str | None = None, plan: str | None = None, banned: bool | None = None, skip: int = 0, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
        import re
        clauses: list[dict[str, Any]] = []
        if query and query.strip():
            regex = re.escape(query.strip())
            clauses.append({"$or": [{field: {"$regex": regex, "$options": "i"}} for field in ("user_id", "phone", "username", "ref_code")]})
        if plan:
            clauses.append({"plan": plan.lower()})
        now = _now_utc()
        if banned is True:
            clauses.append({"banned": True})
            clauses.append({"$or": [{"ban_until": None}, {"ban_until": {"$gt": now}}]})
        elif banned is False:
            clauses.append({"$or": [{"banned": {"$ne": True}}, {"ban_until": {"$lte": now}}]})
        filters: dict[str, Any] = {"$and": clauses} if len(clauses) > 1 else (clauses[0] if clauses else {})
        total = int(self._users.count_documents(filters))
        cursor = self._users.find(filters).sort("created_at", -1).skip(max(0, int(skip))).limit(max(1, min(int(limit), 100)))
        return [self._clean(doc) or {} for doc in cursor], total

    def list_active_trial_users(self, *, query: str | None = None, skip: int = 0, limit: int = 50, now: datetime | None = None) -> tuple[list[dict[str, Any]], int]:
        import re
        current = _aware(now) or _now_utc()
        clauses: list[dict[str, Any]] = [
            {"trial_end": {"$gt": current}},
            {"$or": [{"plan": {"$ne": "premium"}}, {"plan_end": {"$lte": current}}, {"plan_end": None}]},
            {"$or": [{"banned": {"$ne": True}}, {"ban_until": {"$lte": current}}]},
        ]
        if query and query.strip():
            regex = re.escape(query.strip())
            clauses.append({"$or": [{field: {"$regex": regex, "$options": "i"}} for field in ("user_id", "phone", "username", "ref_code")]})
        filters: dict[str, Any] = {"$and": clauses}
        total = int(self._users.count_documents(filters))
        cursor = self._users.find(filters).sort("created_at", -1).skip(max(0, int(skip))).limit(max(1, min(int(limit), 100)))
        return [self._clean(doc) or {} for doc in cursor], total

    def create_user_if_absent(self, user: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        from pymongo.errors import DuplicateKeyError
        doc = dict(user)
        try:
            self._users.insert_one(doc)
            return doc, True
        except DuplicateKeyError:
            existing = self.get_user_by_phone(str(doc["phone"]))
            if existing:
                return existing, False
            raise ValueError("TELEGRAM_ALREADY_LINKED")

    def update_user(self, user_id: str, fields: Mapping[str, Any]) -> dict[str, Any] | None:
        from pymongo import ReturnDocument
        try:
            doc = self._users.find_one_and_update({"user_id": str(user_id)}, {"$set": dict(fields)}, return_document=ReturnDocument.AFTER)
        except Exception as exc:
            try:
                from pymongo.errors import DuplicateKeyError
                if isinstance(exc, DuplicateKeyError):
                    raise ValueError("TELEGRAM_ALREADY_LINKED") from exc
            except ImportError:
                pass
            raise
        return self._clean(doc)

    def add_watchlist_symbol(self, user_id: str, symbol: str, *, max_items: int = 10) -> tuple[dict[str, Any] | None, str]:
        from pymongo import ReturnDocument
        query = {
            "user_id": str(user_id),
            "watchlist": {"$ne": symbol},
            "$expr": {"$lt": [{"$size": {"$ifNull": ["$watchlist", []]}}, max(1, int(max_items))]},
        }
        doc = self._users.find_one_and_update(
            query,
            {"$addToSet": {"watchlist": symbol}, "$set": {"watchlist_updated_at": _now_utc()}},
            return_document=ReturnDocument.AFTER,
        )
        if doc is not None:
            return self._clean(doc), "ADDED"
        user = self.get_user_by_id(str(user_id))
        if user is None:
            return None, "USER_NOT_FOUND"
        symbols = list(user.get("watchlist") or [])
        if symbol in symbols:
            return user, "DUPLICATE"
        if len(symbols) >= max(1, int(max_items)):
            return user, "FULL"
        # The user changed between the read and update; report conflict instead of silently losing data.
        return user, "CONFLICT"

    def remove_watchlist_symbol(self, user_id: str, symbol: str) -> tuple[dict[str, Any] | None, bool]:
        from pymongo import ReturnDocument
        before = self.get_user_by_id(str(user_id))
        if before is None:
            return None, False
        if symbol not in list(before.get("watchlist") or []):
            return before, False
        doc = self._users.find_one_and_update(
            {"user_id": str(user_id), "watchlist": symbol},
            {"$pull": {"watchlist": symbol}, "$set": {"watchlist_updated_at": _now_utc()}},
            return_document=ReturnDocument.AFTER,
        )
        return (self._clean(doc) if doc is not None else self.get_user_by_id(str(user_id))), doc is not None

    def update_alert_preferences(self, user_id: str, fields: Mapping[str, bool]) -> dict[str, Any] | None:
        from pymongo import ReturnDocument
        update = {f"alert_prefs.{str(key)}": bool(value) for key, value in fields.items()}
        update["alert_prefs_updated_at"] = _now_utc()
        return self._clean(self._users.find_one_and_update(
            {"user_id": str(user_id)}, {"$set": update}, return_document=ReturnDocument.AFTER,
        ))

    def list_users_for_alerts(self, *, limit: int = 500, after_user_id: str | None = None) -> list[dict[str, Any]]:
        """Read alert recipients by indexed cursor instead of loading a fixed first N users."""
        projection = {
            "user_id": 1, "telegram_chat_id": 1, "alert_prefs": 1, "plan": 1,
            "plan_end": 1, "trial_end": 1, "banned": 1, "ban_until": 1,
            "is_admin": 1, "watchlist": 1, "language": 1,
        }
        query: dict[str, Any] = {}
        if after_user_id:
            query["user_id"] = {"$gt": str(after_user_id)}
        cursor = self._users.find(query, projection).sort("user_id", 1).limit(max(1, min(int(limit), 2000)))
        return [self._clean(doc) or {} for doc in cursor]

    def extend_premium_plan(self, user_id: str, days: int, event: Mapping[str, Any], *, now: datetime) -> tuple[dict[str, Any] | None, bool]:
        from pymongo import ReturnDocument
        event_doc = dict(event)
        event_id = str(event_doc["event_id"])
        # A single Mongo update pipeline computes the extension from the latest
        # stored plan_end, so concurrent grants cannot overwrite each other.
        # The same atomic update records the idempotency key and audit event.
        active_end = {
            "$cond": [
                {"$and": [
                    {"$eq": [{"$ifNull": ["$plan", "free"]}, "premium"]},
                    {"$gt": [{"$ifNull": ["$plan_end", now]}, now]},
                ]},
                "$plan_end",
                {"$literal": now},
            ]
        }
        next_end = {"$add": [active_end, int(days) * 86400 * 1000]}
        event_expr = {"$mergeObjects": [
            {"$literal": event_doc},
            {"effective_start": active_end, "effective_end": next_end},
        ]}
        pipeline = [{"$set": {
            "plan": "premium",
            "plan_end": next_end,
            "plan_updated_at": {"$literal": now},
            "plan_history": {"$slice": [
                {"$concatArrays": [{"$ifNull": ["$plan_history", []]}, [event_expr]]}, -100,
            ]},
            "processed_plan_event_ids": {"$concatArrays": [
                {"$ifNull": ["$processed_plan_event_ids", []]}, [{"$literal": event_id}],
            ]},
        }}]
        updated = self._users.find_one_and_update(
            {"user_id": str(user_id), "processed_plan_event_ids": {"$ne": event_id}},
            pipeline,
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return self._clean(updated), True
        existing = self.get_user_by_id(str(user_id))
        if existing and event_id in (existing.get("processed_plan_event_ids") or []):
            return existing, False
        return existing, False

    def list_users_for_plan_maintenance(self, *, since: datetime, until: datetime, limit: int = 1000) -> list[dict[str, Any]]:
        query = {"$or": [
            {"trial_end": {"$gte": since, "$lte": until}},
            {"plan": "premium", "plan_end": {"$gte": since, "$lte": until}},
        ]}
        cursor = self._users.find(query).sort([("trial_end", 1), ("plan_end", 1)]).limit(max(1, min(int(limit), 5000)))
        return [self._clean(doc) or {} for doc in cursor]

    def claim_plan_notice(self, user_id: str, kind: str, boundary: datetime, *, now: datetime, lease_until: datetime) -> tuple[str, str] | None:
        import uuid
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError
        boundary_utc = _aware(boundary)
        boundary_key = boundary_utc.isoformat() if boundary_utc else str(boundary)
        dedupe_key = f"{user_id}:{kind}:{boundary_key}"
        claim_id = uuid.uuid4().hex
        doc = {
            "dedupe_key": dedupe_key, "user_id": str(user_id), "kind": str(kind),
            "boundary": boundary_utc, "status": "claimed", "claim_id": claim_id,
            "lease_until": lease_until, "updated_at": now,
        }
        try:
            self._plan_notifications.insert_one(doc)
            return dedupe_key, claim_id
        except DuplicateKeyError:
            updated = self._plan_notifications.find_one_and_update(
                {"dedupe_key": dedupe_key, "status": {"$ne": "sent"}, "lease_until": {"$lte": now}},
                {"$set": {"status": "claimed", "claim_id": claim_id, "lease_until": lease_until, "updated_at": now}},
                return_document=ReturnDocument.AFTER,
            )
            return (dedupe_key, claim_id) if updated else None

    def finish_plan_notice(self, dedupe_key: str, claim_id: str, *, sent: bool, now: datetime, error: str | None = None) -> bool:
        update: dict[str, Any] = {"$set": {"status": "sent" if sent else "pending", "updated_at": now, "lease_until": now}}
        if sent:
            update["$set"]["sent_at"] = now
            update["$unset"] = {"last_error": ""}
        elif error:
            update["$set"]["last_error"] = str(error)[:200]
        result = self._plan_notifications.update_one({"dedupe_key": dedupe_key, "claim_id": claim_id, "status": "claimed"}, update)
        return bool(result.modified_count)

    def create_session(self, session: Mapping[str, Any]) -> None:
        self._sessions.insert_one(dict(session))

    @staticmethod
    def _refresh_match(refresh_hash: str, grace_seconds: int, now: datetime) -> dict[str, Any]:
        clauses: list[dict[str, Any]] = [{"refresh_hash": refresh_hash}]
        if grace_seconds > 0:
            clauses.append({"prev_refresh_hash": refresh_hash, "rotated_at": {"$gte": now - timedelta(seconds=grace_seconds)}})
        return {"$or": clauses}

    def get_session_by_refresh_hash(self, refresh_hash: str, *, grace_seconds: int = 0) -> dict[str, Any] | None:
        now = _now_utc()
        query = {**self._refresh_match(refresh_hash, grace_seconds, now), "revoked_at": {"$exists": False}, "expires_at": {"$gt": now}}
        return self._clean(self._sessions.find_one(query))

    def rotate_session(self, session_id: str, old_hash: str, new_hash: str, *, now: datetime, expires_at: datetime, grace_seconds: int = 0) -> dict[str, Any] | None:
        from pymongo import ReturnDocument
        doc = self._sessions.find_one_and_update(
            {"id": str(session_id), **self._refresh_match(old_hash, grace_seconds, now), "revoked_at": {"$exists": False}, "expires_at": {"$gt": now}},
            # Aggregation-pipeline update (MongoDB >= 4.2): the token being replaced
            # becomes the grace-window token, whichever of the two was presented.
            [{"$set": {"prev_refresh_hash": "$refresh_hash", "rotated_at": now, "refresh_hash": new_hash, "expires_at": expires_at, "last_activity": now}}],
            return_document=ReturnDocument.AFTER,
        )
        return self._clean(doc)

    def revoke_session_by_refresh_hash(self, refresh_hash: str, *, now: datetime) -> bool:
        result = self._sessions.update_one({"$or": [{"refresh_hash": refresh_hash}, {"prev_refresh_hash": refresh_hash}], "revoked_at": {"$exists": False}}, {"$set": {"revoked_at": now, "last_activity": now}, "$unset": {"refresh_hash": "", "prev_refresh_hash": ""}})
        return bool(result.modified_count)

    def revoke_session(self, session_id: str, user_id: str, *, now: datetime) -> bool:
        result = self._sessions.update_one({"id": str(session_id), "user_id": str(user_id), "revoked_at": {"$exists": False}}, {"$set": {"revoked_at": now, "last_activity": now}, "$unset": {"refresh_hash": "", "prev_refresh_hash": ""}})
        return bool(result.modified_count)

    def is_session_active(self, session_id: str, user_id: str, *, now: datetime) -> bool:
        return self._sessions.find_one({"id": str(session_id), "user_id": str(user_id), "revoked_at": {"$exists": False}, "expires_at": {"$gt": now}}, {"_id": 1}) is not None

    def list_active_sessions(self, user_id: str, *, now: datetime) -> list[dict[str, Any]]:
        return [self._clean(doc) or {} for doc in self._sessions.find({"user_id": str(user_id), "revoked_at": {"$exists": False}, "expires_at": {"$gt": now}}).sort("last_activity", -1)]

    def health(self) -> dict[str, Any]:
        self._client.admin.command("ping")
        return {"status": "ok", "adapter": "mongodb", "persistent": True}

    def close(self) -> None:
        self._client.close()


def auth_repository_from_environment() -> AuthRepository:
    environment = os.getenv("FTRADE_ENV", "development").strip().lower()
    if os.getenv("MONGODB_URI"):
        return MongoAuthRepository.from_env()
    if environment == "production":
        raise RuntimeError("FTRADE_ENV=production requiere MONGODB_URI y DATABASE_NAME")
    return InMemoryAuthRepository()
