"""Minimal HS256 access-token validation and plan/ban gate for analytical endpoints.

Token issuance/refresh belongs to the authentication phase; this module only verifies
short-lived signed access tokens and can be wired to that issuer without test bypasses.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from domain.plans import evaluate_access
from api.services.auth import is_admin_user, is_admin_id

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: str
    claims: dict[str, Any]
    is_admin: bool = False


def _decode_segment(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def verify_access_token(token: str) -> Principal:
    secret = os.getenv("ACCESS_TOKEN_SECRET") or os.getenv("JWT_SECRET_KEY")
    if not secret:
        raise HTTPException(status_code=503, detail={"code": "AUTH_NOT_CONFIGURED", "message": "La validación de sesiones aún no está configurada."})
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "Token inválido."})
    head_seg, payload_seg, signature_seg = parts
    try:
        header = json.loads(_decode_segment(head_seg))
        claims = json.loads(_decode_segment(payload_seg))
        supplied = _decode_segment(signature_seg)
    except Exception as exc:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "Token inválido."}) from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "Token inválido."})
    if header.get("alg") != "HS256":
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "Algoritmo de token no admitido."})
    message = f"{head_seg}.{payload_seg}".encode("ascii")
    expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "Token inválido."})
    now = time.time()
    expires = claims.get("exp")
    issued = claims.get("iat")
    try:
        max_ttl = max(60, min(3600, int(os.getenv("ACCESS_TOKEN_MAX_TTL_SECONDS", "900"))))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail={"code": "AUTH_NOT_CONFIGURED", "message": "ACCESS_TOKEN_MAX_TTL_SECONDS no está configurado correctamente."}) from exc
    if (not isinstance(expires, (int, float)) or not math.isfinite(float(expires)) or float(expires) <= now):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "La sesión ha expirado."})
    if (not isinstance(issued, (int, float)) or not math.isfinite(float(issued))
            or float(issued) > now + 30 or float(expires) <= float(issued)
            or float(expires) - float(issued) > max_ttl):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "La duración o la fecha de emisión del token no son válidas."})
    user_id = claims.get("sub") or claims.get("user_id")
    if not isinstance(user_id, (str, int)) or not str(user_id).strip():
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "Token sin identidad de usuario."})
    return Principal(user_id=str(user_id), claims=claims, is_admin=bool(claims.get("is_admin", False)))


def current_principal(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "Se requiere Bearer token."}, headers={"WWW-Authenticate": "Bearer"})
    principal = verify_access_token(credentials.credentials)
    issuer = principal.claims.get("iss")
    production = os.getenv("FTRADE_ENV", "development").strip().lower() == "production"
    # Production must not accept legacy signed tokens: they do not reference a
    # persisted session and therefore cannot be checked for revocation or current
    # account entitlements on every request. Legacy compatibility is development-only.
    if production and issuer != "ftrade-web":
        raise HTTPException(
            status_code=401,
            detail={"code": "LEGACY_TOKEN_NOT_ALLOWED", "message": "La sesión debe renovarse con el servicio actual de KERYNTIX MARKET DESK."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Tokens minted by the KERYNTIX MARKET DESK web client reference a persisted refresh session; the
    # session, account status, plan and ban are checked on each request.
    if issuer == "ftrade-web":
        session_id = principal.claims.get("sid")
        repository = getattr(request.app.state, "auth_repository", None)
        if not session_id or repository is None:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "La sesión no es válida."})
        now = datetime.now(timezone.utc)
        try:
            session_active = repository.is_session_active(str(session_id), principal.user_id, now=now)
            if not session_active:
                raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "La sesión fue revocada o venció."})
            # Access rights are resolved from the current user record on every request,
            # so a new ban or an expired plan does not wait for the JWT to expire.
            user = repository.get_user_by_id(principal.user_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "SESSION_STORE_UNAVAILABLE", "message": "No se pudo validar la sesión con el servicio de cuentas."}) from exc
        if not user:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "La cuenta no existe."})
        claims = dict(principal.claims)
        claims.update({
            "plan": str(user.get("plan", "free")),
            "trial_end": user.get("trial_end"),
            "plan_end": user.get("plan_end"),
            "created_at": user.get("created_at"),
            "plan_updated_at": user.get("plan_updated_at"),
            "plan_history": user.get("plan_history") or [],
            "banned": bool(user.get("banned", False)),
            "ban_until": user.get("ban_until"),
            "ban_reason": user.get("ban_reason"),
            "tz": str(user.get("tz") or "America/Havana"),
        })
        principal = Principal(user_id=principal.user_id, claims=claims, is_admin=is_admin_user(user))
    elif os.getenv("FTRADE_ENV", "development").strip().lower() == "production":
        # On production, admin privileges never come from client-controlled claims;
        # only server-side ADMIN_USER_IDS / ADMIN_PHONE_NUMBERS can grant them.
        principal = Principal(user_id=principal.user_id, claims=principal.claims,
                              is_admin=is_admin_id(principal.user_id))
    return principal


def require_analytical_access(principal: Principal = Depends(current_principal)) -> Principal:
    """Use the canonical plan evaluator for every analytical endpoint."""
    access = evaluate_access(principal.claims, now=datetime.now(timezone.utc), is_admin=principal.is_admin)
    if access["allowed"]:
        return principal
    if access["reason"] == "BANNED":
        detail: dict[str, Any] = {"code": "BANNED", "message": "La cuenta está bloqueada."}
        if principal.claims.get("ban_reason"):
            detail["reason"] = str(principal.claims.get("ban_reason"))
        if access.get("ban_until"):
            detail["ban_until"] = access["ban_until"]
        raise HTTPException(status_code=403, detail=detail)
    raise HTTPException(status_code=403, detail={"code": "ACCESS_EXPIRED", "message": "El periodo de prueba o la suscripción ha vencido."})
