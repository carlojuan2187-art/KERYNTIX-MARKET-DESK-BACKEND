"""Support-ticket business rules; every read/write is scoped to owner or admin."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

UTC = timezone.utc
VALID_STATUSES = {"open", "in_progress", "waiting_user", "closed"}
VALID_PRIORITIES = {"normal", "high"}


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


def _public(doc: dict[str, Any]) -> dict[str, Any]:
    result = dict(doc)
    for key in ("created_at", "updated_at", "closed_at"):
        if key in result:
            result[key] = _iso(result.get(key))
    for message in result.get("messages", []):
        if isinstance(message, dict) and "created_at" in message:
            message["created_at"] = _iso(message.get("created_at"))
    return result


class SupportService:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def create(self, user_id: str, subject: str, message: str, *, now: datetime | None = None) -> dict[str, Any]:
        current = now or _now()
        clean_subject, clean_message = subject.strip(), message.strip()
        if not 5 <= len(clean_subject) <= 120:
            raise HTTPException(status_code=422, detail={"code": "INVALID_SUBJECT", "message": "El asunto debe tener entre 5 y 120 caracteres."})
        if not 10 <= len(clean_message) <= 5000:
            raise HTTPException(status_code=422, detail={"code": "INVALID_MESSAGE", "message": "El mensaje debe tener entre 10 y 5000 caracteres."})
        ticket_id = "tkt_" + uuid.uuid4().hex[:20]
        ticket = {"ticket_id": ticket_id, "user_id": str(user_id), "subject": clean_subject,
                  "status": "open", "priority": "normal", "created_at": current, "updated_at": current,
                  "messages": [{"message_id": uuid.uuid4().hex, "sender_id": str(user_id), "sender_role": "user",
                                "message": clean_message, "created_at": current}]}
        return _public(self.repository.create_ticket(ticket))

    def list_mine(self, user_id: str, *, status: str | None = None, limit: int = 50, before: datetime | None = None) -> dict[str, Any]:
        if status and status not in VALID_STATUSES:
            raise HTTPException(status_code=422, detail={"code": "INVALID_TICKET_STATUS", "message": "Estado de ticket no válido."})
        docs = self.repository.list_tickets(user_id=str(user_id), status=status, limit=limit, before=before)
        return {"items": [_public(doc) for doc in docs], "count": len(docs)}

    def get_mine(self, user_id: str, ticket_id: str) -> dict[str, Any]:
        doc = self.repository.get_ticket(ticket_id, str(user_id))
        if not doc:
            raise HTTPException(status_code=404, detail={"code": "TICKET_NOT_FOUND", "message": "No se encontró el ticket."})
        return _public(doc)

    def message(self, ticket_id: str, sender_id: str, sender_role: str, message: str, *, admin: bool = False, close: bool = False, status_override: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        current = now or _now()
        clean_message = message.strip()
        if not 2 <= len(clean_message) <= 5000:
            raise HTTPException(status_code=422, detail={"code": "INVALID_MESSAGE", "message": "El mensaje debe tener entre 2 y 5000 caracteres."})
        ticket = self.repository.get_ticket(ticket_id, None if admin else str(sender_id))
        if not ticket:
            raise HTTPException(status_code=404, detail={"code": "TICKET_NOT_FOUND", "message": "No se encontró el ticket."})
        if ticket.get("status") == "closed" and not admin:
            raise HTTPException(status_code=409, detail={"code": "TICKET_CLOSED", "message": "El ticket está cerrado. Solicita soporte para reabrirlo."})
        entry = {"message_id": uuid.uuid4().hex, "sender_id": str(sender_id), "sender_role": sender_role,
                 "message": clean_message, "created_at": current}
        new_status = "closed" if close else (status_override or ("waiting_user" if admin else "in_progress"))
        if new_status not in VALID_STATUSES:
            raise HTTPException(status_code=422, detail={"code": "INVALID_TICKET_STATUS", "message": "Estado de ticket no válido."})
        if not admin and status_override is not None:
            raise HTTPException(status_code=403, detail={"code": "STATUS_CHANGE_FORBIDDEN", "message": "Solo soporte puede cambiar el estado del ticket."})
        fields = {"status": new_status, "updated_at": current, "last_message_at": current,
                  "last_message_by": sender_role, "closed_at": current if new_status == "closed" else None}
        updated = self.repository.append_ticket_message(ticket_id, entry, fields, user_id=None if admin else str(sender_id))
        if not updated:
            raise HTTPException(status_code=404, detail={"code": "TICKET_NOT_FOUND", "message": "No se encontró el ticket."})
        return _public(updated)

    def list_admin(self, *, status: str | None = None, limit: int = 50, before: datetime | None = None) -> dict[str, Any]:
        if status and status not in VALID_STATUSES:
            raise HTTPException(status_code=422, detail={"code": "INVALID_TICKET_STATUS", "message": "Estado de ticket no válido."})
        docs = self.repository.list_tickets(status=status, limit=limit, before=before)
        return {"items": [_public(doc) for doc in docs], "count": len(docs)}
