from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.business import InMemoryBusinessRepository
from api.repositories.signals import InMemorySignalRepository
from api.services.auth import issue_access_token
from api.services.payments import amount_to_units, is_valid_address
from api.services.referrals import ReferralService
from api.services.plans import PlanService
from main import create_app

UTC = timezone.utc
WALLET = "0x" + "a" * 40
SENDER = "0x" + "b" * 40


class FakeTelegram:
    def bot_username(self):
        return "keryntix_market_desk_auth_test"

    def is_configured(self):
        return True

    def send_message(self, chat_id, text, *, reply_markup=None):
        return None


class FakeRest:
    async def close(self):
        return None


class FakeChain:
    def __init__(self):
        self.transfer = None
        self.configured = True

    def is_configured(self):
        return self.configured

    def find_matching_transfer(self, sender, payment):
        return self.transfer


def put_user(repo, user_id, *, plan="free", referred_by=None, trial_end=None, now=None):
    now = now or datetime.now(UTC)
    doc = {
        "user_id": str(user_id), "phone": f"+1787{abs(hash(str(user_id))) % 10**7:07d}",
        "telegram_chat_id": str(5000 + len(str(user_id)) + abs(hash(str(user_id))) % 100000),
        "plan": plan, "plan_end": now + timedelta(days=7) if plan == "premium" else None,
        "trial_end": trial_end or (now + timedelta(days=3)),
        "ref_code": f"ref_{user_id}", "referred_by": str(referred_by) if referred_by else None,
        "risk_profile": {"capital": 1000.0, "risk_pct": 1.0}, "watchlist": [],
        "alert_prefs": {"on": True, "liq": True, "mov": True, "vol": True},
        "tz": "America/Puerto_Rico", "banned": False, "created_at": now, "last_activity": now,
    }
    # Tests construct distinct fake identities; use update if collision on generated phone is possible.
    repo.create_user_if_absent(doc)
    session_id = f"session_{user_id}"
    repo.create_session({"id": session_id, "user_id": str(user_id), "refresh_hash": f"refresh_{user_id}",
                         "created_at": now, "expires_at": now + timedelta(days=1), "last_activity": now,
                         "device": "test", "location": "test"})
    token = issue_access_token(repo.get_user_by_id(str(user_id)), session_id, now=now)
    return token, doc


def build_client(monkeypatch, *, admin=False, chain=None):
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "test-secret-for-billing-phase-fifteen-0123456789")
    monkeypatch.setenv("AUTH_CODE_PEPPER", "test-code-pepper-for-phase-fifteen")
    monkeypatch.setenv("FTRADE_ENV", "development")
    monkeypatch.setenv("PAYMENT_WALLET_ADDRESS", WALLET)
    monkeypatch.setenv("PREMIUM_PRICE_USDT", "40.00")
    admin_id = "usr_admin_15" if admin else ""
    monkeypatch.setenv("ADMIN_USER_IDS", admin_id)
    auth_repo = InMemoryAuthRepository()
    biz_repo = InMemoryBusinessRepository()
    user_token, _ = put_user(auth_repo, "usr_customer_15")
    admin_token = None
    if admin:
        admin_token, _ = put_user(auth_repo, admin_id, plan="premium")
    app = create_app(InMemorySignalRepository([]), market_cache=InMemoryJsonCache(),
                     market_rest_client=FakeRest(), auth_repository=auth_repo,
                     telegram_gateway=FakeTelegram(), business_repository=biz_repo,
                     chain_verifier=chain or FakeChain())
    return TestClient(app), auth_repo, biz_repo, user_token, admin_token


def test_premium_price_default_is_ten_usdt_when_environment_is_unset(monkeypatch):
    monkeypatch.delenv("PREMIUM_PRICE_USDT", raising=False)
    app = create_app(
        InMemorySignalRepository([]), market_cache=InMemoryJsonCache(),
        auth_repository=InMemoryAuthRepository(), market_rest_client=FakeRest(),
        chain_verifier=FakeChain(),
    )
    with TestClient(app) as client:
        response = client.get("/billing/plans")
    assert response.status_code == 200
    assert response.json()["plans"][0]["price_usdt"] == "10.000"


def test_payment_confirmation_activates_premium_once(monkeypatch):
    chain = FakeChain()
    client, auth_repo, biz_repo, token, _ = build_client(monkeypatch, chain=chain)
    with client:
        created = client.post("/billing/payments", headers={"Authorization": f"Bearer {token}"})
        assert created.status_code == 200, created.text
        payment = created.json()["payment"]
        assert payment["status"] == "pending"
        assert payment["network"] == "BNB Smart Chain (BEP20)"
        assert payment["amount_usdt"].startswith("40.")
        assert payment["wallet"] == WALLET

        chain.transfer = {"txid": "0x" + "c" * 64 + ":1", "tx_hash": "0x" + "c" * 64,
                          "log_index": 1, "block_number": 12345, "timestamp": int(datetime.now(UTC).timestamp()),
                          "value_units": payment["amount_units"] * 10**12}
        verified = client.post(f"/billing/payments/{payment['payment_id']}/verify", json={"sender_address": SENDER},
                               headers={"Authorization": f"Bearer {token}"})
        assert verified.status_code == 200, verified.text
        assert verified.json()["result"] == "ok"
        saved = auth_repo.get_user_by_id("usr_customer_15")
        assert saved["plan"] == "premium"
        original_end = saved["plan_end"]
        assert biz_repo.get_payment_tx(chain.transfer["txid"])

        again = client.post(f"/billing/payments/{payment['payment_id']}/verify", json={"sender_address": SENDER},
                            headers={"Authorization": f"Bearer {token}"})
        assert again.status_code == 200
        assert auth_repo.get_user_by_id("usr_customer_15")["plan_end"] == original_end


def test_payment_txid_cannot_be_reused_for_another_user(monkeypatch):
    chain = FakeChain()
    client, repo, biz, token, _ = build_client(monkeypatch, chain=chain)
    with client:
        first = client.post("/billing/payments", headers={"Authorization": f"Bearer {token}"}).json()["payment"]
        chain.transfer = {"txid": "0x" + "d" * 64 + ":0", "tx_hash": "0x" + "d" * 64,
                          "log_index": 0, "block_number": 99, "timestamp": int(datetime.now(UTC).timestamp()),
                          "value_units": first["amount_units"] * 10**12}
        ok = client.post(f"/billing/payments/{first['payment_id']}/verify", json={"sender_address": SENDER},
                         headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200, ok.text
        user2_token, _ = put_user(repo, "usr_customer_16")
        second = client.post("/billing/payments", headers={"Authorization": f"Bearer {user2_token}"}).json()["payment"]
        chain.transfer = {**chain.transfer, "value_units": second["amount_units"] * 10**12}
        conflict = client.post(f"/billing/payments/{second['payment_id']}/verify", json={"sender_address": SENDER},
                               headers={"Authorization": f"Bearer {user2_token}"})
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "TX_ALREADY_USED"


def test_admin_manual_activation_is_idempotent_and_audited(monkeypatch):
    client, repo, biz, user_token, admin_token = build_client(monkeypatch, admin=True)
    with client:
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        body = {"days": 30, "reason": "Compensación aprobada por soporte", "request_id": "req_12345678"}
        first = client.post("/admin/users/usr_customer_15/premium", json=body, headers=admin_headers)
        assert first.status_code == 200, first.text
        end1 = repo.get_user_by_id("usr_customer_15")["plan_end"]
        second = client.post("/admin/users/usr_customer_15/premium", json=body, headers=admin_headers)
        assert second.status_code == 200, second.text
        assert repo.get_user_by_id("usr_customer_15")["plan_end"] == end1
        assert any(event["action"] == "premium_activate" for event in biz.list_audit())

        unauth_user = client.post("/admin/users/usr_customer_15/ban", json={"days": 1, "reason": "test ban"},
                                  headers={"Authorization": f"Bearer {user_token}"})
        assert unauth_user.status_code == 403


def test_admin_ban_and_unban_are_audited_and_admin_cannot_ban_admin(monkeypatch):
    client, repo, biz, _user_token, admin_token = build_client(monkeypatch, admin=True)
    with client:
        headers = {"Authorization": f"Bearer {admin_token}"}
        banned = client.post("/admin/users/usr_customer_15/ban", json={"days": 2, "reason": "Violación de reglas"}, headers=headers)
        assert banned.status_code == 200, banned.text
        user = repo.get_user_by_id("usr_customer_15")
        assert user["banned"] is True and user["ban_until"] is not None
        unbanned = client.post("/admin/users/usr_customer_15/unban", json={"reason": "Revisión completada"}, headers=headers)
        assert unbanned.status_code == 200
        assert repo.get_user_by_id("usr_customer_15")["banned"] is False
        forbidden = client.post("/admin/users/usr_admin_15/ban", json={"days": None, "reason": "No permitido"}, headers=headers)
        assert forbidden.status_code == 409
        assert len(biz.list_audit()) >= 2


def test_five_premium_referrals_apply_exactly_one_30_day_reward(monkeypatch):
    monkeypatch.setenv("ADMIN_USER_IDS", "")
    auth_repo = InMemoryAuthRepository()
    biz_repo = InMemoryBusinessRepository()
    now = datetime.now(UTC)
    referrer = {"user_id": "usr_referrer", "phone": "+17875550001", "telegram_chat_id": "7001", "plan": "free",
                "trial_end": now - timedelta(days=1), "plan_end": None, "ref_code": "ref_referrer",
                "risk_profile": {"capital": 1000, "risk_pct": 1}, "watchlist": [], "alert_prefs": {}, "tz": "UTC", "banned": False}
    auth_repo.create_user_if_absent(referrer)
    plan_service = PlanService(auth_repo)
    from api.services.referrals import ReferralService
    service = ReferralService(auth_repo, biz_repo, plan_service)
    for idx in range(5):
        uid = f"usr_referred_{idx}"
        user = {"user_id": uid, "phone": f"+17875551{idx:04d}", "telegram_chat_id": str(7100 + idx), "plan": "premium",
                "plan_end": now + timedelta(days=30), "trial_end": None, "ref_code": f"ref_{uid}", "referred_by": "usr_referrer",
                "risk_profile": {"capital": 1000, "risk_pct": 1}, "watchlist": [], "alert_prefs": {}, "tz": "UTC", "banned": False}
        auth_repo.create_user_if_absent(user)
        service.on_premium_activated(uid, event_id=f"payment:{idx}", now=now)
    saved = auth_repo.get_user_by_id("usr_referrer")
    assert saved["plan"] == "premium"
    assert saved["plan_end"] >= now + timedelta(days=29)
    assert len([r for r in biz_repo.list_referrals("usr_referrer") if r.get("reward_applied")]) == 5
    # Replaying the fifth activation must not issue another reward batch.
    service.on_premium_activated("usr_referred_4", event_id="payment:4", now=now)
    assert len(biz_repo.list_pending_referral_rewards("usr_referrer")) == 0
    assert len(biz_repo.referral_rewards) == 1


def test_support_ticket_is_owner_scoped_and_admin_can_reply(monkeypatch):
    client, repo, biz, user_token, admin_token = build_client(monkeypatch, admin=True)
    with client:
        user_headers = {"Authorization": f"Bearer {user_token}"}
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        created = client.post("/support/tickets", json={"subject": "Problema con mi suscripción", "message": "Necesito ayuda para revisar mi acceso Premium."}, headers=user_headers)
        assert created.status_code == 201, created.text
        ticket = created.json()
        ticket_id = ticket["ticket_id"]
        assert ticket["status"] == "open"
        assert client.get(f"/support/tickets/{ticket_id}", headers=admin_headers).status_code == 404
        admin_list = client.get("/admin/support/tickets", headers=admin_headers)
        assert admin_list.status_code == 200 and admin_list.json()["count"] == 1
        reply = client.post(f"/admin/support/tickets/{ticket_id}/reply", json={"message": "Ya estamos revisando el estado de su cuenta."}, headers=admin_headers)
        assert reply.status_code == 200, reply.text
        user_view = client.get(f"/support/tickets/{ticket_id}", headers=user_headers)
        assert user_view.status_code == 200
        assert len(user_view.json()["messages"]) == 2


def test_address_and_amount_helpers_are_strict():
    assert is_valid_address(WALLET)
    assert not is_valid_address("0x123")
    assert amount_to_units(__import__("decimal").Decimal("40.001")) == 40001000


def test_verified_payment_recovers_activation_after_temporary_failure(monkeypatch):
    chain = FakeChain()
    client, auth_repo, biz_repo, token, _ = build_client(monkeypatch, chain=chain)
    with client:
        created = client.post("/billing/payments", headers={"Authorization": f"Bearer {token}"}).json()["payment"]
        chain.transfer = {"txid": "0x" + "e" * 64 + ":2", "tx_hash": "0x" + "e" * 64,
                          "log_index": 2, "block_number": 456, "timestamp": int(datetime.now(UTC).timestamp()),
                          "value_units": created["amount_units"] * 10**12}
        plan_service = client.app.state.plan_service
        original_activate = plan_service.activate_premium
        calls = {"count": 0}

        def flaky_activate(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary persistence problem")
            return original_activate(*args, **kwargs)

        plan_service.activate_premium = flaky_activate
        response = client.post(f"/billing/payments/{created['payment_id']}/verify", json={"sender_address": SENDER},
                               headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        assert response.json()["result"] == "activation_pending"
        stored = biz_repo.get_payment(created["payment_id"])
        assert stored["status"] == "verified"
        assert stored["activation_status"] == "pending"
        assert auth_repo.get_user_by_id("usr_customer_15")["plan"] == "free"

        # A later current-payment request resumes activation from persisted state;
        # no second chain scan is needed and the unique tx claim stays intact.
        plan_service.activate_premium = original_activate
        resumed = client.get("/billing/payments/current", headers={"Authorization": f"Bearer {token}"})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["payment"]["status"] == "completed"
        assert auth_repo.get_user_by_id("usr_customer_15")["plan"] == "premium"
        assert len(biz_repo.payment_txs) == 1


def test_admin_summary_and_payment_history_are_admin_only(monkeypatch):
    client, auth_repo, biz_repo, user_token, admin_token = build_client(monkeypatch, admin=True)
    with client:
        created = client.post("/billing/payments", headers={"Authorization": f"Bearer {user_token}"})
        assert created.status_code == 200, created.text
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        summary = client.get("/admin/summary", headers=admin_headers)
        assert summary.status_code == 200, summary.text
        body = summary.json()
        assert body["users_total"] >= 2
        assert body["payments_total"] == 1
        assert body["payments_pending"] == 1
        payments = client.get("/admin/payments?status=pending", headers=admin_headers)
        assert payments.status_code == 200, payments.text
        item = payments.json()["items"][0]
        assert item["payment_id"] == created.json()["payment"]["payment_id"]
        assert item["user_id"] == "usr_customer_15"
        assert "verification_lease_until" not in item
        assert client.get("/admin/payments?status=bogus", headers=admin_headers).status_code == 422
        assert client.get("/admin/summary", headers={"Authorization": f"Bearer {user_token}"}).status_code == 403
        assert client.get("/admin/payments", headers={"Authorization": f"Bearer {user_token}"}).status_code == 403


def test_bad_chain_transfer_response_never_credits_plan(monkeypatch):
    chain = FakeChain()
    client, auth_repo, biz_repo, token, _ = build_client(monkeypatch, chain=chain)
    with client:
        payment = client.post("/billing/payments", headers={"Authorization": f"Bearer {token}"}).json()["payment"]
        chain.transfer = {"txid": "0x" + "f" * 64 + ":0", "tx_hash": "0x" + "f" * 64,
                          "log_index": 0, "block_number": 100, "timestamp": int(datetime.now(UTC).timestamp()),
                          "value_units": (payment["amount_units"] + 1) * 10**12}
        response = client.post(f"/billing/payments/{payment['payment_id']}/verify", json={"sender_address": SENDER},
                               headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "INVALID_CHAIN_RESPONSE"
        assert auth_repo.get_user_by_id("usr_customer_15")["plan"] == "free"
        assert biz_repo.get_payment_tx(chain.transfer["txid"]) is None
        assert biz_repo.get_payment(payment["payment_id"])["status"] == "pending"


def test_admin_can_retry_only_verified_payment_activation(monkeypatch):
    client, auth_repo, biz_repo, user_token, admin_token = build_client(monkeypatch, admin=True)
    with client:
        pending = client.post("/billing/payments", headers={"Authorization": f"Bearer {user_token}"}).json()["payment"]
        payment_id = pending["payment_id"]
        txid = "0x" + "9" * 64 + ":7"
        biz_repo.update_payment(payment_id, {"status": "verified", "txid": txid, "tx_hash": txid.split(":")[0],
                                            "log_index": 7, "activation_status": "pending", "verified_at": datetime.now(UTC)})
        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        retried = client.post(f"/admin/payments/{payment_id}/retry-activation", headers=admin_headers)
        assert retried.status_code == 200, retried.text
        assert retried.json()["result"] == "ok"
        assert retried.json()["payment"]["status"] == "completed"
        assert auth_repo.get_user_by_id("usr_customer_15")["plan"] == "premium"
        assert any(event["action"] == "payment_activation_retry" for event in biz_repo.list_audit())
        # Completed payments are safe to inspect/replay: they must not extend Premium again.
        plan_end = auth_repo.get_user_by_id("usr_customer_15")["plan_end"]
        retried_again = client.post(f"/admin/payments/{payment_id}/retry-activation", headers=admin_headers)
        assert retried_again.status_code == 200
        assert auth_repo.get_user_by_id("usr_customer_15")["plan_end"] == plan_end
        forbidden = client.post(f"/admin/payments/{payment_id}/retry-activation", headers={"Authorization": f"Bearer {user_token}"})
        assert forbidden.status_code == 403


def test_stale_payment_verification_lease_can_be_reclaimed(monkeypatch):
    now = datetime.now(UTC)
    biz_repo = InMemoryBusinessRepository()
    document = {"payment_id": "pay_stale_lease", "user_id": "usr_customer_lease", "status": "pending",
                "expected_units": 40001000, "created_at": now, "expires_at": now + timedelta(minutes=10),
                "attempts": 0}
    biz_repo.create_payment(document)
    first = biz_repo.claim_payment_verification("pay_stale_lease", "usr_customer_lease", now=now,
                                                lease_until=now + timedelta(seconds=30), sender=SENDER)
    assert first is not None and first["attempts"] == 1
    blocked = biz_repo.claim_payment_verification("pay_stale_lease", "usr_customer_lease", now=now + timedelta(seconds=10),
                                                  lease_until=now + timedelta(seconds=40), sender=SENDER)
    assert blocked is None
    reclaimed = biz_repo.claim_payment_verification("pay_stale_lease", "usr_customer_lease", now=now + timedelta(seconds=31),
                                                    lease_until=now + timedelta(seconds=90), sender=SENDER)
    assert reclaimed is not None and reclaimed["attempts"] == 2
    assert reclaimed["status"] == "verifying"


def test_referral_summary_links_to_frontend_registration_page(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "test-secret-for-referral-links")
    monkeypatch.setenv("FRONTEND_URL", "https://ftrade-web-production.up.railway.app/")
    auth_repo = InMemoryAuthRepository()
    biz_repo = InMemoryBusinessRepository()
    put_user(auth_repo, "usr_referral_link")
    service = ReferralService(auth_repo, biz_repo, PlanService(auth_repo))

    summary = service.summary("usr_referral_link")

    assert summary["referral_link"] == "https://keryntix-market-desk-production.up.railway.app/r/ref_usr_referral_link"
    assert "t.me/" not in summary["referral_link"]
