from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.business import InMemoryBusinessRepository
from api.services.payments import PaymentService
from api.services.plans import PlanService
from api.services.referrals import ReferralService
from worker.business_reconciler import BusinessReconciler

UTC = timezone.utc


class NoChain:
    def is_configured(self):
        return False

    def find_matching_transfer(self, sender, payment):
        raise AssertionError("La reconciliación no debe consultar transferencias no verificadas")


def _user(user_id: str, *, referred_by: str | None = None, plan: str = "free", now: datetime) -> dict:
    return {
        "user_id": user_id,
        "phone": f"+1787{abs(hash(user_id)) % 10**7:07d}",
        "telegram_chat_id": None,
        "plan": plan,
        "plan_end": now + timedelta(days=7) if plan == "premium" else None,
        "trial_end": now + timedelta(days=2),
        "referred_by": referred_by,
        "ref_code": f"ref_{user_id}",
        "risk_profile": {"capital": 1000.0, "risk_pct": 1.0},
        "watchlist": [],
        "alert_prefs": {},
        "tz": "UTC",
        "banned": False,
        "created_at": now,
    }


def test_reconciler_expires_stale_payments_and_completes_verified_activation_and_referral_reward():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    auth = InMemoryAuthRepository()
    business = InMemoryBusinessRepository()
    auth.create_user_if_absent(_user("payer-1", now=now))
    auth.create_user_if_absent(_user("referrer-1", now=now))
    plan_service = PlanService(auth)
    referral_service = ReferralService(auth, business, plan_service)
    payments = PaymentService(business, plan_service, NoChain(), referral_service=referral_service)

    # An already verified transfer is recoverable without another chain lookup.
    business.payments["verified-payment"] = {
        "payment_id": "verified-payment", "user_id": "payer-1", "status": "verified",
        "activation_status": "pending", "txid": "0xabc:0", "tx_hash": "0xabc",
        "expected_units": 40_001_000, "verified_at": now - timedelta(minutes=5),
        "created_at": now - timedelta(minutes=6), "updated_at": now - timedelta(minutes=5),
    }
    # A stale, unverified payment is expired, releasing its reservation fields.
    business.payments["expired-payment"] = {
        "payment_id": "expired-payment", "user_id": "another-user", "status": "pending",
        "expected_units": 40_002_000, "expires_at": now - timedelta(seconds=1),
        "active_slot": "another-user", "active_amount": 40_002_000,
        "verification_lease_until": now - timedelta(seconds=10), "created_at": now - timedelta(hours=4),
    }

    for idx in range(5):
        business.record_referral({
            "referral_id": f"referral-{idx}", "referrer_id": "referrer-1",
            "referred_id": f"referred-{idx}", "activated_plan": "premium",
            "activated_at": now - timedelta(minutes=5-idx), "reward_applied": False,
        })
    reward = business.claim_referral_reward("referrer-1", now=now)
    assert reward and reward["status"] == "pending"

    reconciler = BusinessReconciler(business, payments, referral_service, InMemoryJsonCache())
    status = asyncio.run(reconciler.run_once(now=now))

    assert status["payments_expired"] == 1
    assert status["verified_activations_completed"] == 1
    assert business.payments["verified-payment"]["status"] == "completed"
    assert "active_slot" not in business.payments["expired-payment"]
    assert "active_amount" not in business.payments["expired-payment"]
    assert auth.get_user_by_id("payer-1")["plan"] == "premium"
    assert auth.get_user_by_id("referrer-1")["plan"] == "premium"
    assert business.referral_rewards[reward["reward_id"]]["status"] == "applied"


def test_reconciler_retries_failed_verified_activation_idempotently():
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    auth = InMemoryAuthRepository()
    business = InMemoryBusinessRepository()
    auth.create_user_if_absent(_user("payer-retry", now=now))
    base_plan_service = PlanService(auth)

    class FlakyPlanService:
        def __init__(self):
            self.fail_once = True

        def activate_premium(self, *args, **kwargs):
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("temporary storage outage")
            return base_plan_service.activate_premium(*args, **kwargs)

        def snapshot(self, *args, **kwargs):
            return base_plan_service.snapshot(*args, **kwargs)

    plan = FlakyPlanService()
    referrals = ReferralService(auth, business, plan)  # no referral is attached to this user
    payments = PaymentService(business, plan, NoChain(), referral_service=referrals)
    business.payments["retry-payment"] = {
        "payment_id": "retry-payment", "user_id": "payer-retry", "status": "verified",
        "activation_status": "pending", "txid": "0xdef:0", "tx_hash": "0xdef",
        "expected_units": 40_001_000, "verified_at": now, "created_at": now, "updated_at": now,
    }
    reconciler = BusinessReconciler(business, payments, referrals, InMemoryJsonCache())

    first = asyncio.run(reconciler.run_once(now=now))
    assert first["verified_activations_pending"] == 1
    assert business.payments["retry-payment"]["status"] == "verified"
    assert business.payments["retry-payment"]["activation_status"] == "pending"

    second = asyncio.run(reconciler.run_once(now=now + timedelta(seconds=30)))
    assert second["verified_activations_completed"] == 1
    user = auth.get_user_by_id("payer-retry")
    assert user["plan"] == "premium"
    assert len(user.get("plan_history", [])) == 1
