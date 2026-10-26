"""Admin account management with explicit access checks and auditable mutations."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from api.repositories.auth import AuthRepository
from api.services.auth import is_admin_user
from api.services.plans import PlanService

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    result = dict(user)
    result.pop("_id", None)
    result.pop("processed_plan_event_ids", None)
    for key in ("created_at", "last_activity", "trial_end", "plan_end", "ban_until", "banned_at", "plan_updated_at"):
        if key in result:
            result[key] = _iso(result.get(key))
    result["is_admin"] = is_admin_user(user)
    return result


class AdminService:
    def __init__(self, auth_repository: AuthRepository, business_repository: Any, plan_service: PlanService) -> None:
        self.auth_repository = auth_repository
        self.repository = business_repository
        self.plan_service = plan_service

    def list_users(self, *, query: str | None, plan: str | None, banned: bool | None, skip: int, limit: int) -> dict[str, Any]:
        docs, total = self.auth_repository.list_users(query=query, plan=plan, banned=banned, skip=skip, limit=limit)
        return {"items": [public_user(doc) for doc in docs], "total": total, "skip": max(0, skip), "limit": max(1, min(limit, 100))}

    def list_active_trial_users(self, *, query: str | None, skip: int, limit: int, now: datetime | None = None) -> dict[str, Any]:
        docs, total = self.auth_repository.list_active_trial_users(query=query, skip=skip, limit=limit, now=now or _now())
        return {"items": [public_user(doc) for doc in docs], "total": total, "skip": max(0, skip), "limit": max(1, min(limit, 100))}

    def get_user(self, user_id: str) -> dict[str, Any]:
        user = self.auth_repository.get_user_by_id(str(user_id))
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        result = public_user(user)
        result["plan_history"] = user.get("plan_history", [])[-50:]
        return result

    def activate_premium(self, user_id: str, actor_id: str, *, days: int, reason: str, request_id: str) -> dict[str, Any]:
        if not 5 <= len(reason.strip()) <= 500:
            raise HTTPException(status_code=422, detail={"code": "INVALID_REASON", "message": "Indica una razón de 5 a 500 caracteres."})
        result = self.plan_service.activate_premium(str(user_id), days=days, source="admin", actor_id=str(actor_id), reference=reason.strip(), idempotency_key=f"admin:{request_id}")
        self.repository.write_audit({"event_id": uuid.uuid4().hex, "actor_id": str(actor_id), "action": "premium_activate", "target_user_id": str(user_id), "details": {"days": days, "reason": reason.strip(), "event_id": result.get("event_id"), "activation_applied": result.get("activation_applied")}, "created_at": _now()})
        return result

    def ban_user(self, user_id: str, actor_id: str, *, days: int | None, reason: str) -> dict[str, Any]:
        user_id, actor_id = str(user_id), str(actor_id)
        reason = reason.strip()
        if not 5 <= len(reason) <= 500:
            raise HTTPException(status_code=422, detail={"code": "INVALID_REASON", "message": "Indica una razón de 5 a 500 caracteres."})
        if days is not None and (isinstance(days, bool) or not 1 <= days <= 365):
            raise HTTPException(status_code=422, detail={"code": "INVALID_BAN_DURATION", "message": "El baneo temporal debe durar entre 1 y 365 días; null significa permanente."})
        current = _now()
        user = self.auth_repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        if user_id == actor_id or is_admin_user(user):
            raise HTTPException(status_code=409, detail={"code": "ADMIN_CANNOT_BE_BANNED", "message": "No se puede banear a esta cuenta de administración."})
        until = current + timedelta(days=days) if days is not None else None
        updated = self.auth_repository.update_user(user_id, {"banned": True, "ban_until": until, "banned_at": current, "banned_by": actor_id, "ban_reason": reason, "updated_at": current})
        self.repository.write_audit({"event_id": uuid.uuid4().hex, "actor_id": actor_id, "action": "user_ban", "target_user_id": user_id, "details": {"days": days, "reason": reason, "ban_until": _iso(until)}, "created_at": current})
        return public_user(updated or user)

    def ban_user_until(self, user_id: str, actor_id: str, *, until: datetime | None, reason: str) -> dict[str, Any]:
        """Apply the exact UTC expiry supplied by the API contract; None means permanent."""
        user_id, actor_id = str(user_id), str(actor_id)
        reason = reason.strip()
        if not 5 <= len(reason) <= 500:
            raise HTTPException(status_code=422, detail={"code": "INVALID_REASON", "message": "Indica una razón de 5 a 500 caracteres."})
        current = _now()
        normalized_until = None
        if until is not None:
            normalized_until = until.replace(tzinfo=UTC) if until.tzinfo is None else until.astimezone(UTC)
            seconds = (normalized_until - current).total_seconds()
            if seconds <= 0 or seconds > 365 * 86400:
                raise HTTPException(status_code=422, detail={"code": "INVALID_BAN_DURATION", "message": "until debe ser futura y no superar 365 días."})
        user = self.auth_repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        if user_id == actor_id or is_admin_user(user):
            raise HTTPException(status_code=409, detail={"code": "ADMIN_CANNOT_BE_BANNED", "message": "No se puede banear a esta cuenta de administración."})
        updated = self.auth_repository.update_user(user_id, {"banned": True, "ban_until": normalized_until,
            "banned_at": current, "banned_by": actor_id, "ban_reason": reason, "updated_at": current})
        self.repository.write_audit({"event_id": uuid.uuid4().hex, "actor_id": actor_id, "action": "user_ban", "target_user_id": user_id,
            "details": {"until": _iso(normalized_until), "permanent": normalized_until is None, "reason": reason}, "created_at": current})
        return public_user(updated or user)

    def unban_user(self, user_id: str, actor_id: str, *, reason: str) -> dict[str, Any]:
        user_id, actor_id, reason = str(user_id), str(actor_id), reason.strip()
        if not 5 <= len(reason) <= 500:
            raise HTTPException(status_code=422, detail={"code": "INVALID_REASON", "message": "Indica una razón de 5 a 500 caracteres."})
        user = self.auth_repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        if user_id == actor_id or is_admin_user(user):
            raise HTTPException(status_code=409, detail={"code": "ADMIN_CANNOT_BE_BANNED", "message": "No se puede banear a esta cuenta de administración."})
        now = _now()
        updated = self.auth_repository.update_user(user_id, {"banned": False, "ban_until": None, "unbanned_at": now, "unbanned_by": actor_id, "unban_reason": reason, "updated_at": now})
        self.repository.write_audit({"event_id": uuid.uuid4().hex, "actor_id": actor_id, "action": "user_unban", "target_user_id": user_id, "details": {"reason": reason}, "created_at": now})
        return public_user(updated or user)

    def summary(self) -> dict[str, Any]:
        current = _now()
        return {**self.auth_repository.admin_user_summary(now=current), **self.repository.admin_summary(current),
                "generated_at": _iso(current)}

    def audit(self, *, limit: int = 100, before: datetime | None = None) -> dict[str, Any]:
        items = self.repository.list_audit(limit=limit, before=before)
        return {"items": [{**item, "created_at": _iso(item.get("created_at"))} for item in items], "count": len(items)}
