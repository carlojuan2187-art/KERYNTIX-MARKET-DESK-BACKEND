"""Canonical plan, trial, and account-access rules shared by API and workers.

Access is calculated at request time from persisted UTC timestamps. We do not
need a scheduler to flip a user's plan flag exactly at expiry: expiration is a
pure time-based rule, which avoids stale JWTs and delayed scheduler runs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

UTC = timezone.utc


def as_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = as_utc(value)
    return normalized.isoformat().replace("+00:00", "Z") if normalized else None


def evaluate_access(user: Mapping[str, Any], *, now: datetime | None = None, is_admin: bool = False) -> dict[str, Any]:
    """Resolve the current entitlement using the original KERYNTIX MARKET DESK access rule.

    Administrators are authorized by server-side configuration and bypass plan
    and ban gates. Non-admin users are blocked by an active ban; otherwise they
    get the full Premium feature set while either their Premium subscription or
    five-day trial is active. The trial is not a limited/free tier. Once both
    entitlements expire, analytical/platform features are blocked while account
    and billing remain available so the user can renew. A stored `banned=True`
    with an expired `ban_until` is not an active ban.
    """
    current = as_utc(now or datetime.now(UTC)) or datetime.now(UTC)
    plan = str(user.get("plan") or "free").strip().lower()
    if plan not in {"free", "premium"}:
        plan = "free"
    plan_end = as_utc(user.get("plan_end"))
    trial_end = as_utc(user.get("trial_end"))
    ban_until = as_utc(user.get("ban_until"))
    stored_banned = bool(user.get("banned", False))
    ban_active = stored_banned and (ban_until is None or ban_until > current)
    premium_active = plan == "premium" and plan_end is not None and plan_end > current
    # The trial carries exactly the same feature permissions as a paid Premium
    # plan until its persisted expiry timestamp. The expiry boundary is exclusive.
    trial_active = trial_end is not None and trial_end > current

    if is_admin:
        state = "admin"
        allowed = True
        reason = None
        access_until = None
    elif ban_active:
        state = "banned"
        allowed = False
        reason = "BANNED"
        access_until = None
    elif premium_active:
        state = "premium"
        allowed = True
        reason = None
        access_until = plan_end
    elif trial_active:
        state = "trial"
        allowed = True
        reason = None
        access_until = trial_end
    else:
        state = "expired"
        allowed = False
        reason = "ACCESS_EXPIRED"
        access_until = None

    # Keep the existing access model: all analytical features share this gate.
    # Account and billing routes remain available so an expired user can recover.
    features = {
        "account": True,
        "billing": True,
        "analytics": allowed,
        "signals": allowed,
        "market_radar": allowed,
        "derivatives": allowed,
        "statistics": allowed,
    }
    return {
        "allowed": allowed,
        "status": state,
        "reason": reason,
        "plan": plan,
        "premium_active": premium_active,
        "trial_active": trial_active,
        "banned": stored_banned,
        "ban_active": ban_active,
        "ban_until": iso_utc(ban_until),
        "trial_end": iso_utc(trial_end),
        "plan_end": iso_utc(plan_end),
        "access_until": iso_utc(access_until),
        "features": features,
    }
