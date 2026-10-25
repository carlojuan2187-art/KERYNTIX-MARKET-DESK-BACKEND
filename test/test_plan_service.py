from __future__ import annotations

from datetime import datetime, timedelta, timezone

from api.repositories.auth import InMemoryAuthRepository
from api.services.plans import PlanService

UTC = timezone.utc


def seed_user(repository: InMemoryAuthRepository, *, user_id="usr_plan_1", plan="free", trial_end=None, plan_end=None, chat_id="77"):
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    doc = {
        "user_id": user_id, "phone": f"+1787555{len(user_id):04d}", "telegram_chat_id": chat_id,
        "plan": plan, "trial_end": trial_end, "plan_end": plan_end,
        "ref_code": f"ref_{user_id}", "created_at": now,
    }
    repository.create_user_if_absent(doc)
    return doc


def test_premium_extension_stacks_on_current_active_expiry_and_is_idempotent():
    repository = InMemoryAuthRepository()
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    current_end = now + timedelta(days=10)
    seed_user(repository, plan="premium", plan_end=current_end, trial_end=now - timedelta(days=1))
    service = PlanService(repository)

    first = service.activate_premium("usr_plan_1", days=30, source="payment", reference="tx-1", idempotency_key="payment:tx-1", now=now)
    second = service.activate_premium("usr_plan_1", days=30, source="payment", reference="tx-1", idempotency_key="payment:tx-1", now=now)

    assert first["activation_applied"] is True
    assert first["plan_end"] == (current_end + timedelta(days=30)).isoformat().replace("+00:00", "Z")
    assert second["activation_applied"] is False
    assert second["plan_end"] == first["plan_end"]
    user = repository.get_user_by_id("usr_plan_1")
    assert len(user["plan_history"]) == 1
    assert user["processed_plan_event_ids"] == ["payment:tx-1"]


def test_expired_premium_extension_starts_from_now_not_old_plan_end():
    repository = InMemoryAuthRepository()
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    seed_user(repository, plan="premium", plan_end=now - timedelta(days=60), trial_end=now - timedelta(days=55))
    result = PlanService(repository).activate_premium("usr_plan_1", days=30, source="admin", actor_id="admin-1", idempotency_key="admin:grant-1", now=now)
    assert result["plan_end"] == (now + timedelta(days=30)).isoformat().replace("+00:00", "Z")
    assert result["state"] == "premium" and result["allowed"] is True


def test_invalid_plan_extension_is_rejected():
    from fastapi import HTTPException
    import pytest

    repository = InMemoryAuthRepository()
    seed_user(repository)
    with pytest.raises(HTTPException) as error:
        PlanService(repository).activate_premium("usr_plan_1", days=366, source="admin", idempotency_key="admin:bad")
    assert error.value.status_code == 422


def test_plan_snapshot_is_derived_from_current_clock_and_not_persisted_status():
    repository = InMemoryAuthRepository()
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    seed_user(repository, plan="premium", plan_end=now + timedelta(seconds=1))
    service = PlanService(repository)
    assert service.snapshot("usr_plan_1", now=now)["allowed"] is True
    expired = service.snapshot("usr_plan_1", now=now + timedelta(seconds=2))
    assert expired["state"] == "expired" and expired["allowed"] is False
    # The record can still show its historical `plan=premium`; access itself is time-derived.
    assert repository.get_user_by_id("usr_plan_1")["plan"] == "premium"
