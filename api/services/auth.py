"""Business logic for phone + Telegram authentication and account sessions."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, Request, Response, status

from api.repositories.auth import AuthRepository
from domain.plans import evaluate_access
from api.services.telegram import TelegramBotAPI, TelegramDeliveryError

UTC = timezone.utc
ACCESS_COOKIE_NAME = "ftrade_refresh"
REFRESH_DAYS = 30


def refresh_in_body_enabled() -> bool:
    """Return refresh tokens in JSON only for deployments that need the cookie fallback.

    Keep the default enabled for existing cross-origin Railway deployments. Once the
    frontend and API share a site and HttpOnly cookies are verified, set
    AUTH_REFRESH_IN_BODY=false and VITE_AUTH_COOKIE_ONLY=true to remove JS storage.
    """
    return os.getenv("AUTH_REFRESH_IN_BODY", "true").strip().lower() not in {"0", "false", "no", "off"}


def refresh_cookie_samesite() -> str:
    production = os.getenv("FTRADE_ENV", "development").strip().lower() == "production"
    default = "none" if production else "lax"
    value = os.getenv("AUTH_COOKIE_SAMESITE", default).strip().lower()
    if value not in {"lax", "strict", "none"}:
        raise RuntimeError("AUTH_COOKIE_SAMESITE debe ser lax, strict o none")
    return value


def refresh_grace_seconds() -> int:
    """Window in which the just-rotated refresh token is still accepted once.

    Covers a lost rotation response (page reload / closed tab / timeout) that would
    otherwise log the user out. 0 disables the grace window.
    """
    try:
        return max(0, min(300, int(os.getenv("AUTH_REFRESH_GRACE_SECONDS", "60"))))
    except ValueError:
        return 60
CHALLENGE_MINUTES = 5
RESEND_SECONDS = 60
MAX_CODE_ATTEMPTS = 5
PASSWORD_HASH_ITERATIONS = 310_000


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except ValueError:
            return None
    return None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_placeholder(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    return (not normalized) or any(marker in normalized for marker in ("replace-with", "your_new", "replace_me", "placeholder", "change-me", "changeme"))


def _auth_pepper() -> bytes:
    secret = os.getenv("AUTH_CODE_PEPPER") or os.getenv("ACCESS_TOKEN_SECRET") or os.getenv("JWT_SECRET_KEY")
    if _is_placeholder(secret):
        raise HTTPException(status_code=503, detail={"code": "AUTH_NOT_CONFIGURED", "message": "Configura AUTH_CODE_PEPPER con un secreto real."})
    return str(secret).encode("utf-8")


def hash_code(phone: str, code: str) -> str:
    return hmac.new(_auth_pepper(), f"{phone}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


def constant_time_code_match(phone: str, supplied: str, expected_hash: str | None) -> bool:
    if not expected_hash:
        return False
    try:
        actual = hash_code(phone, supplied)
    except HTTPException:
        return False
    return hmac.compare_digest(actual, expected_hash)


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 and a unique random salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_HASH_ITERATIONS
    )
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        algorithm, iterations_raw, salt_hex, digest_hex = stored_hash.split("$", 3)
        iterations = int(iterations_raw)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 1_000_000:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        if len(salt) < 16 or len(expected) != 32:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _b64url(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _access_secret() -> str:
    secret = os.getenv("ACCESS_TOKEN_SECRET") or os.getenv("JWT_SECRET_KEY")
    if _is_placeholder(secret):
        raise HTTPException(status_code=503, detail={"code": "AUTH_NOT_CONFIGURED", "message": "ACCESS_TOKEN_SECRET debe configurarse con un secreto real."})
    if os.getenv("FTRADE_ENV", "development").lower() == "production" and len(secret) < 32:
        raise HTTPException(status_code=503, detail={"code": "AUTH_NOT_CONFIGURED", "message": "ACCESS_TOKEN_SECRET debe tener al menos 32 caracteres en producción."})
    return secret


def _admin_ids() -> set[str]:
    return {item.strip() for item in os.getenv("ADMIN_USER_IDS", "").split(",") if item.strip()}


def _normalize_admin_phone(value: Any) -> str:
    """Normalize phone values to + followed by digits for environment matching."""
    import re
    digits = re.sub(r"\D", "", str(value or ""))
    if not 8 <= len(digits) <= 15:
        return ""
    return "+" + digits


def _admin_phone_numbers() -> set[str]:
    return {
        normalized
        for item in os.getenv("ADMIN_PHONE_NUMBERS", "").split(",")
        if (normalized := _normalize_admin_phone(item))
    }


def is_admin_id(user_id: str) -> bool:
    """Legacy admin check by FTrade's internal user ID."""
    return str(user_id) in _admin_ids()


def is_admin_user(user: Mapping[str, Any] | None) -> bool:
    """Resolve admin access using a configured internal ID or verified account phone.

    Phone-based administration is safe here because account.phone is populated only
    after the phone + Telegram verification flow completes. This helper must receive
    the persisted user record, not request-supplied phone data.
    """
    if not user:
        return False
    if is_admin_id(str(user.get("user_id", ""))):
        return True
    configured_phones = _admin_phone_numbers()
    user_phone = _normalize_admin_phone(user.get("phone"))
    return bool(user_phone and user_phone in configured_phones)


def admin_configured() -> bool:
    return bool(_admin_ids() or _admin_phone_numbers())


def issue_access_token(user: Mapping[str, Any], session_id: str, *, now: datetime | None = None) -> str:
    now = now or utcnow()
    iat = int(now.timestamp())
    max_ttl = max(60, min(3600, int(os.getenv("ACCESS_TOKEN_MAX_TTL_SECONDS", "900"))))
    claims: dict[str, Any] = {
        "iss": "ftrade-web",
        "sub": str(user["user_id"]),
        "sid": str(session_id),
        "jti": secrets.token_urlsafe(12),
        "iat": iat,
        "exp": iat + max_ttl,
        "is_admin": is_admin_user(user),
        "plan": str(user.get("plan", "free")),
        "trial_end": iso_utc(as_utc(user.get("trial_end"))),
        "plan_end": iso_utc(as_utc(user.get("plan_end"))),
        "banned": bool(user.get("banned", False)),
        "ban_until": iso_utc(as_utc(user.get("ban_until"))),
        "ban_reason": user.get("ban_reason"),
        "tz": str(user.get("tz") or "America/Havana"),
    }
    header = {"alg": "HS256", "typ": "JWT"}
    body = f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}.{_b64url(json.dumps(claims, separators=(',', ':'), ensure_ascii=False).encode())}"
    import hashlib as _hashlib
    import hmac as _hmac
    signature = _hmac.new(_access_secret().encode("utf-8"), body.encode("ascii"), _hashlib.sha256).digest()
    return f"{body}.{_b64url(signature)}"


def _new_user_id() -> str:
    return "usr_" + secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:18]


def _normalize_user_for_response(user: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(user)
    result.pop("_id", None)
    result.pop("password_hash", None)
    result["language"] = "es" if result.get("language") == "es" else "en"
    # Normalize legacy accounts as well as new accounts so the UI, realtime
    # handlers, and notification worker all see the same default preferences.
    defaults = {
        "on": True, "liq": True, "mov": True, "vol": True, "signals": True,
        "price": True, "plans": True, "in_app": True, "telegram": True,
        "web_push": False,
    }
    stored_prefs = result.get("alert_prefs")
    normalized_prefs = dict(defaults)
    if isinstance(stored_prefs, Mapping):
        for key in defaults:
            if key in stored_prefs:
                normalized_prefs[key] = bool(stored_prefs[key])
    result["alert_prefs"] = normalized_prefs
    for key in ("created_at", "last_activity", "trial_end", "plan_end", "ban_until", "banned_at"):
        result[key] = iso_utc(as_utc(result.get(key))) if result.get(key) is not None else None
    result["is_admin"] = is_admin_user(user)
    # Avoid exposing internal moderation note storage under a public profile key.
    result.pop("ban_reason", None)
    return result


def _user_creation_document(request_doc: Mapping[str, Any], referrer: Mapping[str, Any] | None, now: datetime) -> dict[str, Any]:
    user_id = _new_user_id()
    trial_days = max(0, min(30, int(os.getenv("TRIAL_DAYS", "5"))))
    user: dict[str, Any] = {
        "user_id": user_id,
        "phone": str(request_doc["phone"]),
        "language": "es" if request_doc.get("language") == "es" else "en",
        "telegram_chat_id": str(request_doc["telegram_chat_id"]),
        "plan": "free",
        "trial_end": now + timedelta(days=trial_days),
        "ref_code": f"ref_{user_id}",
        "ref_premium_valid": False,
        "risk_profile": {"capital": 1000.0, "risk_pct": 1.0},
        "watchlist": [],
        "alert_prefs": {
            "on": True, "liq": True, "mov": True, "vol": True,
            "signals": True, "price": True, "plans": True,
            "in_app": True, "telegram": True, "web_push": False,
        },
        "tz": "America/Havana",
        "banned": False,
        "created_at": now,
        "last_activity": now,
    }
    if request_doc.get("password_hash"):
        user["password_hash"] = str(request_doc["password_hash"])
    username = request_doc.get("telegram_username")
    if username:
        user["username"] = "@" + str(username).lstrip("@")
    if referrer and str(referrer.get("user_id")) != user_id:
        user["referred_by"] = str(referrer.get("user_id"))
    return user


class AuthService:
    def __init__(self, repository: AuthRepository, telegram: TelegramBotAPI | None = None) -> None:
        self.repository = repository
        self.telegram = telegram or TelegramBotAPI()

    def start_phone_challenge(self, phone: str, *, ref_code: str | None = None, requester_ip: str | None = None) -> dict[str, Any]:
        now = utcnow()
        ip_hash = hash_token(requester_ip) if requester_ip else None
        ip_limit = max(3, min(100, int(os.getenv("PHONE_CHALLENGE_IP_LIMIT", "10"))))
        if ip_hash and self.repository.count_recent_auth_requests_by_ip(ip_hash, since=now - timedelta(hours=1)) >= ip_limit:
            raise HTTPException(status_code=429, detail={"code": "TOO_MANY_REQUESTS", "message": "Se alcanzó el límite de solicitudes de acceso desde esta conexión. Inténtalo más tarde."})
        recent = self.repository.find_recent_phone_request(phone, since=now - timedelta(seconds=RESEND_SECONDS))
        if recent:
            raise HTTPException(status_code=429, detail={"code": "TOO_MANY_REQUESTS", "message": "Espera 60 segundos antes de solicitar otro código."})
        username = self.telegram.bot_username()
        is_configured = getattr(self.telegram, "is_configured", None)
        if not username or (callable(is_configured) and not is_configured()):
            raise HTTPException(status_code=503, detail={"code": "TELEGRAM_NOT_CONFIGURED", "message": "El token y el nombre del bot de autenticación de Telegram deben estar configurados."})
        token = secrets.token_urlsafe(32)
        link_expires = now + timedelta(minutes=CHALLENGE_MINUTES)
        referrer = None
        if ref_code:
            referrer = self.repository.get_user_by_ref_code(ref_code)
        document = {
            "token_hash": hash_token(token),
            "flow": "login",
            "phone": phone,
            "ref_code": ref_code if referrer else None,
            "created_at": now,
            "link_expires_at": link_expires,
            "code_expires_at": None,
            "resend_at": now + timedelta(seconds=RESEND_SECONDS),
            "contact_verified": False,
            "attempts": 0,
            "contact_attempts": 0,
            "requester_ip_hash": ip_hash,
            "consumed": False,
        }
        self.repository.create_auth_request(document)
        return {"token": token, "phone": phone, "step": "awaiting_telegram", "expires_at": iso_utc(link_expires), "resend_at": iso_utc(now + timedelta(seconds=RESEND_SECONDS))}

    def start_registration_challenge(self, phone: str, password: str, *, ref_code: str | None = None, requester_ip: str | None = None, language: str = "en") -> dict[str, Any]:
        """Begin a new account registration; only this flow creates password accounts."""
        if self.repository.get_user_by_phone(phone):
            raise HTTPException(status_code=409, detail={"code": "ACCOUNT_ALREADY_EXISTS", "message": "Ya existe una cuenta con este teléfono. Inicia sesión o recupera tu contraseña."})
        now = utcnow()
        ip_hash = hash_token(requester_ip) if requester_ip else None
        ip_limit = max(3, min(100, int(os.getenv("PHONE_CHALLENGE_IP_LIMIT", "10"))))
        if ip_hash and self.repository.count_recent_auth_requests_by_ip(ip_hash, since=now - timedelta(hours=1)) >= ip_limit:
            raise HTTPException(status_code=429, detail={"code": "TOO_MANY_REQUESTS", "message": "Se alcanzó el límite de registros desde esta conexión. Inténtalo más tarde."})
        recent = self.repository.find_recent_phone_request(phone, since=now - timedelta(seconds=RESEND_SECONDS))
        if recent:
            raise HTTPException(status_code=429, detail={"code": "TOO_MANY_REQUESTS", "message": "Espera 60 segundos antes de solicitar otro código."})
        username = self.telegram.bot_username()
        is_configured = getattr(self.telegram, "is_configured", None)
        if not username or (callable(is_configured) and not is_configured()):
            raise HTTPException(status_code=503, detail={"code": "TELEGRAM_NOT_CONFIGURED", "message": "El token y el nombre del bot de autenticación de Telegram deben estar configurados."})
        token = secrets.token_urlsafe(32)
        link_expires = now + timedelta(minutes=CHALLENGE_MINUTES)
        referrer = self.repository.get_user_by_ref_code(ref_code) if ref_code else None
        self.repository.create_auth_request({
            "token_hash": hash_token(token),
            "flow": "register",
            "phone": phone,
            "password_hash": hash_password(password),
            "language": "es" if language == "es" else "en",
            "ref_code": ref_code if referrer else None,
            "created_at": now,
            "link_expires_at": link_expires,
            "code_expires_at": None,
            "resend_at": now + timedelta(seconds=RESEND_SECONDS),
            "contact_verified": False,
            "attempts": 0,
            "contact_attempts": 0,
            "requester_ip_hash": ip_hash,
            "consumed": False,
        })
        return {"token": token, "phone": phone, "step": "awaiting_telegram", "expires_at": iso_utc(link_expires), "resend_at": iso_utc(now + timedelta(seconds=RESEND_SECONDS))}

    def start_password_recovery_challenge(self, phone: str, *, requester_ip: str | None = None) -> dict[str, Any]:
        # Return the same public response and run the same IP/phone throttles
        # regardless of whether the account exists. The existence check is only
        # used after Telegram ownership is proven, to prevent accidental signup.
        challenge = self.start_phone_challenge(phone, requester_ip=requester_ip)
        if not self.repository.get_user_by_phone(phone):
            marked = self.repository.update_auth_request(
                hash_token(challenge["token"]), {"recovery_noop": True}
            )
            if not marked:
                raise HTTPException(status_code=503, detail={
                    "code": "RECOVERY_UNAVAILABLE",
                    "message": "No se pudo completar la solicitud. Inténtalo de nuevo más tarde.",
                })
        return challenge

    def telegram_check(self, token: str) -> dict[str, Any]:
        request = self.repository.get_auth_request(hash_token(token))
        if request is None:
            raise HTTPException(status_code=404, detail={"code": "AUTH_REQUEST_NOT_FOUND", "message": "La solicitud de acceso no existe."})
        now = utcnow()
        link_expiry = as_utc(request.get("link_expires_at"))
        code_expiry = as_utc(request.get("code_expires_at"))
        linked = bool(request.get("contact_verified"))
        if not linked and (link_expiry is None or link_expiry <= now):
            raise HTTPException(status_code=410, detail={"code": "AUTH_REQUEST_EXPIRED", "message": "La solicitud venció. Vuelve a solicitar el código."})
        if linked and request.get("flow") in {"login", "register"} and (code_expiry is None or code_expiry <= now or request.get("consumed")):
            raise HTTPException(status_code=410, detail={"code": "AUTH_CODE_EXPIRED", "message": "El código venció. Solicita uno nuevo."})
        return {"linked": linked}

    def resend_code(self, token: str) -> dict[str, Any]:
        token_hash = hash_token(token)
        challenge = self.repository.get_auth_request(token_hash)
        if not challenge or challenge.get("flow") not in {"login", "register"}:
            raise HTTPException(status_code=404, detail={"code": "AUTH_REQUEST_NOT_FOUND", "message": "La solicitud de acceso no existe."})
        now = utcnow()
        if not challenge.get("contact_verified") or not challenge.get("telegram_chat_id"):
            raise HTTPException(status_code=409, detail={"code": "TELEGRAM_NOT_VERIFIED", "message": "Primero verifica tu contacto en Telegram."})
        if challenge.get("consumed"):
            raise HTTPException(status_code=410, detail={"code": "AUTH_REQUEST_CONSUMED", "message": "Esta solicitud ya fue utilizada."})
        resend_at = as_utc(challenge.get("resend_at"))
        if resend_at and now < resend_at:
            raise HTTPException(status_code=429, detail={"code": "TOO_MANY_REQUESTS", "message": "Espera antes de solicitar otro código."})
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_expiry = now + timedelta(minutes=CHALLENGE_MINUTES)
        new_resend_at = now + timedelta(seconds=RESEND_SECONDS)
        updated = self.repository.update_auth_request(token_hash, {
            "code_hash": hash_code(str(challenge["phone"]), code),
            "code_expires_at": code_expiry,
            "resend_at": new_resend_at,
            "attempts": 0,
        }, conditions={"contact_verified": True, "consumed": False})
        if not updated:
            raise HTTPException(status_code=410, detail={"code": "AUTH_REQUEST_EXPIRED", "message": "La solicitud ya no está disponible."})
        try:
            self.telegram.send_message(str(challenge["telegram_chat_id"]), f"Tu nuevo código de acceso FTrade es: {code}\nVence en 5 minutos. No lo compartas.")
        except TelegramDeliveryError as exc:
            raise HTTPException(status_code=503, detail={"code": "TELEGRAM_DELIVERY_FAILED", "message": "No se pudo enviar el código por Telegram. Inténtalo de nuevo más tarde."}) from exc
        return {"expires_at": iso_utc(code_expiry), "resend_at": iso_utc(new_resend_at)}

    def verify_code(self, phone: str, code: str, *, request: Request) -> tuple[dict[str, Any], str, str, bool]:
        now = utcnow()
        challenge = self.repository.get_latest_login_request_for_phone(phone)
        if not challenge or not challenge.get("contact_verified") or not challenge.get("telegram_chat_id"):
            raise HTTPException(status_code=409, detail={"code": "TELEGRAM_NOT_VERIFIED", "message": "Primero verifica tu contacto en Telegram."})
        code_expiry = as_utc(challenge.get("code_expires_at"))
        if challenge.get("consumed") or not code_expiry or code_expiry <= now:
            raise HTTPException(status_code=410, detail={"code": "AUTH_CODE_EXPIRED", "message": "El código venció. Solicita uno nuevo."})
        counted = self.repository.increment_auth_attempts(str(challenge["token_hash"]), now=now, field="attempts", maximum=MAX_CODE_ATTEMPTS)
        if not counted:
            raise HTTPException(status_code=429, detail={"code": "TOO_MANY_ATTEMPTS", "message": "Alcanzaste el máximo de 5 intentos. Solicita un nuevo código."})
        if not constant_time_code_match(phone, code, counted.get("code_hash")):
            if int(counted.get("attempts", 0)) >= MAX_CODE_ATTEMPTS:
                raise HTTPException(status_code=429, detail={"code": "TOO_MANY_ATTEMPTS", "message": "Alcanzaste el máximo de 5 intentos. Solicita un nuevo código."})
            remaining = MAX_CODE_ATTEMPTS - int(counted.get("attempts", 0))
            raise HTTPException(status_code=422, detail={"code": "INVALID_CODE", "message": f"Código incorrecto. Te quedan {remaining} intentos."})
        consumed = self.repository.update_auth_request(str(challenge["token_hash"]), {"consumed": True, "consumed_at": now}, conditions={"consumed": False})
        if not consumed:
            raise HTTPException(status_code=410, detail={"code": "AUTH_CODE_EXPIRED", "message": "El código ya fue utilizado. Solicita uno nuevo."})

        existing = self.repository.get_user_by_phone(phone)
        if challenge.get("recovery_noop") and existing is None:
            # Do not let an unknown number become a new account by completing a
            # recovery flow. The browser shows the same neutral confirmation.
            raise HTTPException(status_code=409, detail={
                "code": "RECOVERY_ACCEPTED",
                "message": "Si existe una cuenta con ese teléfono, se han enviado instrucciones para recuperar el acceso.",
            })
        if challenge.get("flow") == "register" and existing:
            raise HTTPException(status_code=409, detail={"code": "ACCOUNT_ALREADY_EXISTS", "message": "Ya existe una cuenta con este teléfono. Inicia sesión o recupera tu contraseña."})
        if existing:
            current_chat = str(existing.get("telegram_chat_id") or "")
            challenge_chat = str(challenge["telegram_chat_id"])
            if current_chat and current_chat != challenge_chat:
                raise HTTPException(status_code=409, detail={"code": "TELEGRAM_ALREADY_LINKED", "message": "Esta cuenta ya está vinculada a otra cuenta de Telegram."})
            owner = self.repository.get_user_by_telegram_chat_id(challenge_chat)
            if owner and str(owner.get("user_id")) != str(existing.get("user_id")):
                raise HTTPException(status_code=409, detail={"code": "TELEGRAM_ALREADY_LINKED", "message": "Esta cuenta de Telegram ya está vinculada a otro usuario."})
            patch: dict[str, Any] = {"last_activity": now}
            if not current_chat:
                patch["telegram_chat_id"] = challenge_chat
            if challenge.get("telegram_username"):
                patch["username"] = "@" + str(challenge["telegram_username"]).lstrip("@")
            try:
                user = self.repository.update_user(str(existing["user_id"]), patch) or existing
            except ValueError as exc:
                if str(exc) == "TELEGRAM_ALREADY_LINKED":
                    raise HTTPException(status_code=409, detail={"code": "TELEGRAM_ALREADY_LINKED", "message": "Esta cuenta de Telegram ya está vinculada a otro usuario."}) from exc
                raise
            is_new = False
        else:
            referrer = self.repository.get_user_by_ref_code(str(challenge.get("ref_code"))) if challenge.get("ref_code") else None
            user_document = _user_creation_document(challenge, referrer, now)
            try:
                user, is_new = self.repository.create_user_if_absent(user_document)
                if challenge.get("flow") == "register" and not is_new:
                    raise HTTPException(status_code=409, detail={"code": "ACCOUNT_ALREADY_EXISTS", "message": "Ya existe una cuenta con este teléfono. Inicia sesión o recupera tu contraseña."})
                if str(user.get("telegram_chat_id") or "") != str(challenge["telegram_chat_id"]):
                    user = self.repository.update_user(str(user["user_id"]), {"telegram_chat_id": str(challenge["telegram_chat_id"]), "last_activity": now}) or user
            except ValueError as exc:
                if str(exc) == "TELEGRAM_ALREADY_LINKED":
                    raise HTTPException(status_code=409, detail={"code": "TELEGRAM_ALREADY_LINKED", "message": "Esta cuenta de Telegram ya está vinculada a otro usuario."}) from exc
                raise

        user_agent = (request.headers.get("user-agent") or "Unknown device")[:200]
        session_id = secrets.token_urlsafe(18)
        refresh_token = secrets.token_urlsafe(48)
        expires_at = now + timedelta(days=REFRESH_DAYS)
        session = {
            "id": session_id,
            "user_id": str(user["user_id"]),
            "refresh_hash": hash_token(refresh_token),
            "created_at": now,
            "expires_at": expires_at,
            "last_activity": now,
            "device": user_agent,
            "location": "Unknown",
        }
        self.repository.create_session(session)
        access = issue_access_token(user, session_id, now=now)
        return _normalize_user_for_response(user), access, refresh_token, is_new

    def login_with_password(self, phone: str, password: str, *, request: Request) -> tuple[dict[str, Any], str, str]:
        now = utcnow()
        requester_ip = request.client.host if request.client else None
        ip_hash = hash_token(requester_ip) if requester_ip else None
        phone_limit = max(3, min(30, int(os.getenv("PASSWORD_LOGIN_PHONE_LIMIT", "10"))))
        ip_limit = max(5, min(100, int(os.getenv("PASSWORD_LOGIN_IP_LIMIT", "30"))))
        since_phone = now - timedelta(minutes=15)
        since_ip = now - timedelta(hours=1)
        if self.repository.count_recent_password_attempts(phone=phone, since=since_phone) >= phone_limit:
            raise HTTPException(status_code=429, detail={"code": "TOO_MANY_ATTEMPTS", "message": "Demasiados intentos para este teléfono. Espera 15 minutos e inténtalo de nuevo."})
        if ip_hash and self.repository.count_recent_password_attempts(ip_hash=ip_hash, since=since_ip) >= ip_limit:
            raise HTTPException(status_code=429, detail={"code": "TOO_MANY_REQUESTS", "message": "Se alcanzó el límite de inicios de sesión desde esta conexión. Inténtalo más tarde."})
        self.repository.create_auth_request({
            "token_hash": hash_token(secrets.token_urlsafe(32)),
            "flow": "password_login",
            "phone": phone,
            "requester_ip_hash": ip_hash,
            "created_at": now,
        })
        user = self.repository.get_user_by_phone(phone)
        if not user:
            raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS", "message": "Teléfono o contraseña incorrectos."})
        stored_hash = user.get("password_hash")
        if not stored_hash:
            raise HTTPException(status_code=409, detail={"code": "PASSWORD_SETUP_REQUIRED", "message": "Esta cuenta es anterior al acceso con contraseña. Configura tu contraseña una sola vez mediante la opción de recuperación."})
        if not verify_password(password, str(stored_hash)):
            raise HTTPException(status_code=401, detail={"code": "INVALID_CREDENTIALS", "message": "Teléfono o contraseña incorrectos."})

        user = self.repository.update_user(str(user["user_id"]), {"last_activity": now}) or user
        user_agent = (request.headers.get("user-agent") or "Unknown device")[:200]
        session_id = secrets.token_urlsafe(18)
        refresh_token = secrets.token_urlsafe(48)
        expires_at = now + timedelta(days=REFRESH_DAYS)
        self.repository.create_session({
            "id": session_id,
            "user_id": str(user["user_id"]),
            "refresh_hash": hash_token(refresh_token),
            "created_at": now,
            "expires_at": expires_at,
            "last_activity": now,
            "device": user_agent,
            "location": "Unknown",
        })
        access = issue_access_token(user, session_id, now=now)
        return _normalize_user_for_response(user), access, refresh_token

    def set_password(self, user_id: str, password: str) -> dict[str, Any]:
        now = utcnow()
        user = self.repository.update_user(user_id, {
            "password_hash": hash_password(password),
            "password_updated_at": now,
            "last_activity": now,
        })
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        return _normalize_user_for_response(user)

    def refresh(self, raw_refresh_token: str | None, fallback_token: str | None = None) -> tuple[str, str, datetime]:
        candidates = [t for t in dict.fromkeys([raw_refresh_token, fallback_token]) if t]
        if not candidates:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "No hay una sesión de renovación válida."})
        grace = refresh_grace_seconds()
        old_hash, session = None, None
        for candidate in candidates:
            candidate_hash = hash_token(candidate)
            session = self.repository.get_session_by_refresh_hash(candidate_hash, grace_seconds=grace)
            if session:
                old_hash = candidate_hash
                break
        if not session or old_hash is None:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "La sesión fue revocada o venció."})
        user = self.repository.get_user_by_id(str(session["user_id"]))
        if not user:
            self.repository.revoke_session_by_refresh_hash(old_hash, now=utcnow())
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "La cuenta asociada ya no existe."})
        now = utcnow()
        new_token = secrets.token_urlsafe(48)
        expires_at = now + timedelta(days=REFRESH_DAYS)
        rotated = self.repository.rotate_session(str(session["id"]), old_hash, hash_token(new_token), now=now, expires_at=expires_at, grace_seconds=grace)
        if not rotated:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "La sesión fue revocada o ya se renovó."})
        return issue_access_token(user, str(session["id"]), now=now), new_token, expires_at

    def me(self, user_id: str) -> dict[str, Any]:
        user = self.repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        return _normalize_user_for_response(user)

    def patch_me(self, user_id: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        updates = dict(fields)
        if "tz" in updates:
            try:
                ZoneInfo(str(updates["tz"]))
            except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
                raise HTTPException(status_code=422, detail={"code": "INVALID_TIMEZONE", "message": "tz debe ser una zona horaria IANA válida."}) from exc
        if "language" in updates and updates["language"] not in {"en", "es"}:
            raise HTTPException(status_code=422, detail={"code": "INVALID_LANGUAGE", "message": "language debe ser en o es."})
        if "exchange" in updates and updates["exchange"] is not None:
            updates["exchange"] = str(updates["exchange"]).strip()
        updates["last_activity"] = utcnow()
        user = self.repository.update_user(user_id, updates)
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        return _normalize_user_for_response(user)

    def create_telegram_link_request(self, user_id: str) -> dict[str, Any]:
        user = self.repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        if user.get("telegram_chat_id"):
            raise HTTPException(status_code=409, detail={"code": "TELEGRAM_ALREADY_LINKED", "message": "Telegram ya está vinculado a esta cuenta."})
        username = self.telegram.bot_username()
        is_configured = getattr(self.telegram, "is_configured", None)
        if not username or (callable(is_configured) and not is_configured()):
            raise HTTPException(status_code=503, detail={"code": "TELEGRAM_NOT_CONFIGURED", "message": "El token y el nombre del bot de autenticación de Telegram deben estar configurados."})
        now = utcnow()
        token = secrets.token_urlsafe(32)
        expiry = now + timedelta(minutes=CHALLENGE_MINUTES)
        self.repository.create_auth_request({
            "token_hash": hash_token(token), "flow": "link", "user_id": str(user_id), "phone": str(user["phone"]),
            "created_at": now, "link_expires_at": expiry, "contact_verified": False,
            "attempts": 0, "contact_attempts": 0, "consumed": False,
        })
        return {"token": token, "deep_link": f"https://t.me/{username}?start=link_{token}", "expires_at": iso_utc(expiry)}

    def unlink_telegram(self, user_id: str) -> dict[str, Any]:
        user = self.repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        updated = self.repository.update_user(user_id, {"telegram_chat_id": None, "username": None, "last_activity": utcnow()})
        return _normalize_user_for_response(updated or user)

    def account_restriction(self, user_id: str) -> dict[str, Any]:
        user = self.repository.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "La cuenta no existe."})
        access = evaluate_access(user, now=utcnow(), is_admin=is_admin_user(user))
        if access["status"] == "banned":
            reason = user.get("ban_reason") or "Cuenta bloqueada por administración."
        elif access["status"] == "expired":
            reason = "El periodo de prueba o la suscripción ha vencido."
        else:
            reason = "Sin restricción de acceso activa."
        return {
            "reason": reason,
            "status": access["status"],
            "allowed": access["allowed"],
            "ban_active": access["ban_active"],
            "ban_until": access["ban_until"],
            "access_until": access["access_until"],
        }

    @staticmethod
    def set_refresh_cookie(response: Response, token: str, *, expires_at: datetime | None = None) -> None:
        secure_setting = os.getenv("AUTH_COOKIE_SECURE")
        production = os.getenv("FTRADE_ENV", "development").lower() == "production"
        secure = True if production else (secure_setting.lower() == "true" if secure_setting is not None else False)
        max_age = int(max(0, (expires_at - utcnow()).total_seconds())) if expires_at else REFRESH_DAYS * 86400
        # Default to None for existing separate Railway hosts. Set Lax when the
        # frontend and API share a site to reduce cross-site cookie exposure.
        same_site = refresh_cookie_samesite()
        response.set_cookie(ACCESS_COOKIE_NAME, token, max_age=max_age, httponly=True, secure=secure, samesite=same_site, path="/auth")

    @staticmethod
    def clear_refresh_cookie(response: Response) -> None:
        secure_setting = os.getenv("AUTH_COOKIE_SECURE")
        production = os.getenv("FTRADE_ENV", "development").lower() == "production"
        secure = True if production else (secure_setting.lower() == "true" if secure_setting is not None else False)
        same_site = refresh_cookie_samesite()
        response.delete_cookie(ACCESS_COOKIE_NAME, path="/auth", httponly=True, secure=secure, samesite=same_site)

    def logout(self, token: str | None) -> bool:
        if not token:
            return False
        return self.repository.revoke_session_by_refresh_hash(hash_token(token), now=utcnow())

    def sessions(self, user_id: str, current_session_id: str | None) -> list[dict[str, Any]]:
        now = utcnow()
        sessions = self.repository.list_active_sessions(user_id, now=now)
        return [{
            "id": str(item.get("id")),
            "device": str(item.get("device") or "Unknown device"),
            "location": str(item.get("location") or "Unknown"),
            "current": bool(current_session_id and str(item.get("id")) == current_session_id),
            "last_activity": iso_utc(as_utc(item.get("last_activity"))) or iso_utc(now),
        } for item in sessions]

    def revoke_session(self, user_id: str, session_id: str) -> None:
        if not self.repository.revoke_session(session_id, user_id, now=utcnow()):
            raise HTTPException(status_code=404, detail={"code": "SESSION_NOT_FOUND", "message": "La sesión no existe o no pertenece a esta cuenta."})


def handle_telegram_contact(repository: AuthRepository, *, token_hash: str, sender_user_id: str, contact_user_id: str | None, chat_id: str, contact_phone: str, username: str | None, now: datetime | None = None) -> dict[str, Any]:
    """Validate Telegram's native shared-contact event and finish the challenge.

    This function is shared by the standalone polling worker and deterministic tests.
    Returns flow metadata; a login code is returned only to the worker and is never
    written in plaintext to the database or included in an HTTP response.
    """
    now = now or utcnow()
    request_doc = repository.get_auth_request(token_hash)
    if not request_doc:
        raise ValueError("AUTH_REQUEST_NOT_FOUND")
    expiry = as_utc(request_doc.get("link_expires_at"))
    if not expiry or expiry <= now:
        raise ValueError("AUTH_REQUEST_EXPIRED")
    if int(request_doc.get("contact_attempts", 0)) >= 5:
        raise ValueError("CONTACT_ATTEMPTS_EXHAUSTED")
    if str(sender_user_id) != str(contact_user_id or ""):
        repository.increment_auth_attempts(token_hash, now=now, field="contact_attempts", maximum=5)
        raise ValueError("CONTACT_OWNER_MISMATCH")
    digits = "".join(ch for ch in contact_phone if ch.isdigit())
    normalized_phone = ("+" + digits) if contact_phone.strip().startswith("+") else ("+" + digits if 8 <= len(digits) <= 15 and not digits.startswith("0") else "")
    if normalized_phone != str(request_doc.get("phone")):
        repository.increment_auth_attempts(token_hash, now=now, field="contact_attempts", maximum=5)
        raise ValueError("PHONE_MISMATCH")

    chat_id = str(chat_id)
    owner = repository.get_user_by_telegram_chat_id(chat_id)
    flow = str(request_doc.get("flow"))
    if flow == "link":
        user = repository.get_user_by_id(str(request_doc.get("user_id")))
        if not user or str(user.get("phone")) != normalized_phone:
            raise ValueError("ACCOUNT_PHONE_MISMATCH")
        if owner and str(owner.get("user_id")) != str(user.get("user_id")):
            raise ValueError("TELEGRAM_ALREADY_LINKED")
        current_chat = str(user.get("telegram_chat_id") or "")
        if current_chat and current_chat != chat_id:
            raise ValueError("TELEGRAM_ALREADY_LINKED")
        repository.update_user(str(user["user_id"]), {"telegram_chat_id": chat_id, "username": ("@" + username.lstrip("@")) if username else user.get("username"), "last_activity": now})
        updated = repository.update_auth_request(token_hash, {"contact_verified": True, "telegram_chat_id": chat_id, "telegram_username": username, "linked_at": now, "consumed": True}, conditions={"contact_verified": False, "link_expires_at_gt": now})
        if not updated:
            raise ValueError("AUTH_REQUEST_EXPIRED")
        return {"flow": "link", "chat_id": chat_id, "code": None}

    existing = repository.get_user_by_phone(normalized_phone)
    if owner and (not existing or str(owner.get("user_id")) != str(existing.get("user_id"))):
        raise ValueError("TELEGRAM_ALREADY_LINKED")
    if existing and existing.get("telegram_chat_id") and str(existing["telegram_chat_id"]) != chat_id:
        raise ValueError("TELEGRAM_ALREADY_LINKED")
    code = f"{secrets.randbelow(1_000_000):06d}"
    updated = repository.update_auth_request(token_hash, {
        "contact_verified": True,
        "telegram_chat_id": chat_id,
        "telegram_username": username,
        "linked_at": now,
        "code_hash": hash_code(normalized_phone, code),
        "code_expires_at": now + timedelta(minutes=CHALLENGE_MINUTES),
        "resend_at": now + timedelta(seconds=RESEND_SECONDS),
        "attempts": 0,
    }, conditions={"contact_verified": False, "link_expires_at_gt": now})
    if not updated:
        raise ValueError("AUTH_REQUEST_EXPIRED")
    return {"flow": "login", "chat_id": chat_id, "code": code}
