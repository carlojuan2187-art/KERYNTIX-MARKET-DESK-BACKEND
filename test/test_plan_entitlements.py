from __future__ import annotations

from datetime import datetime, timedelta, timezone

from domain.plans import evaluate_access

UTC = timezone.utc


def test_trial_and_premium_access_are_resolved_from_utc_dates():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    trial = evaluate_access({"plan": "free", "trial_end": now + timedelta(hours=2)}, now=now)
    premium = evaluate_access({"plan": "premium", "plan_end": now + timedelta(days=30)}, now=now)
    assert trial["allowed"] and trial["status"] == "trial"
    assert trial["features"]["signals"] is True
    assert premium["allowed"] and premium["status"] == "premium"
    assert premium["access_until"] == (now + timedelta(days=30)).isoformat().replace("+00:00", "Z")


def test_expired_trial_or_plan_loses_analytics_but_account_billing_remain_available():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    expired = evaluate_access({"plan": "premium", "plan_end": now - timedelta(seconds=1), "trial_end": now - timedelta(days=1)}, now=now)
    assert expired["allowed"] is False
    assert expired["reason"] == "ACCESS_EXPIRED"
    assert expired["features"]["analytics"] is False
    assert expired["features"]["account"] is True
    assert expired["features"]["billing"] is True


def test_permanent_and_active_temporary_bans_block_non_admins():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    permanent = evaluate_access({"banned": True, "plan": "premium", "plan_end": now + timedelta(days=5)}, now=now)
    temporary = evaluate_access({"banned": True, "ban_until": now + timedelta(hours=2), "trial_end": now + timedelta(days=2)}, now=now)
    assert permanent["status"] == "banned" and not permanent["allowed"]
    assert temporary["status"] == "banned" and not temporary["allowed"]


def test_expired_temporary_ban_is_no_longer_active_and_admin_bypasses_plan_and_ban():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    expired_ban = evaluate_access({"banned": True, "ban_until": now - timedelta(seconds=1), "trial_end": now + timedelta(days=1)}, now=now)
    admin = evaluate_access({"banned": True, "plan": "free", "trial_end": now - timedelta(days=1)}, now=now, is_admin=True)
    assert expired_ban["status"] == "trial" and expired_ban["allowed"]
    assert admin["status"] == "admin" and admin["allowed"]
