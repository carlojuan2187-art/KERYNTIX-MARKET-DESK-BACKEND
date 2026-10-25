from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api.cache import InMemoryJsonCache
from api.repositories.auth import InMemoryAuthRepository
from api.repositories.signals import InMemorySignalRepository
from api.repositories.user_features import InMemoryUserFeatureRepository
from api.security import Principal, current_principal
from main import create_app

UTC = timezone.utc
PUBLIC_KEY = base64.urlsafe_b64encode(b"\x04" + b"\x01" * 64).decode("ascii").rstrip("=")


class FakeGateway:
    def __init__(self, configured=True):
        self.configured = configured
        self.public_key = PUBLIC_KEY

    def is_configured(self):
        return self.configured

    def public_key_for_browser(self):
        if not self.public_key:
            from fastapi import HTTPException
            raise HTTPException(status_code=503, detail={"code": "WEB_PUSH_NOT_CONFIGURED", "message": "not configured"})
        return self.public_key


class FakeRest:
    async def close(self):
        pass


def setup():
    auth = InMemoryAuthRepository()
    user_id = "usr_push_1"
    now = datetime.now(UTC)
    auth.create_user_if_absent({
        "user_id": user_id, "phone": "+17875550101", "telegram_chat_id": None,
        "plan": "premium", "plan_end": now + timedelta(days=30), "trial_end": None,
        "banned": False, "ref_code": "push-test", "alert_prefs": {"on": True, "web_push": True},
    })
    auth.create_user_if_absent({
        "user_id": "usr_push_2", "phone": "+17875550102", "telegram_chat_id": None,
        "plan": "premium", "plan_end": now + timedelta(days=30), "trial_end": None,
        "banned": False, "ref_code": "push-test-2",
    })
    features = InMemoryUserFeatureRepository()
    app = create_app(InMemorySignalRepository([]), market_cache=InMemoryJsonCache(),
                     market_rest_client=FakeRest(), auth_repository=auth,
                     user_feature_repository=features, web_push_gateway=FakeGateway())
    principal = Principal(user_id, {"sub": user_id, "plan": "premium", "plan_end": (now + timedelta(days=30)).isoformat()}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    return app, auth, features, principal


def subscription(endpoint_tail="1", *, host="fcm.googleapis.com"):
    return {
        "endpoint": f"https://{host}/fcm/send/{endpoint_tail}" + "x" * 70,
        "expirationTime": None,
        "keys": {"p256dh": "A" * 87, "auth": "B" * 22},
    }


def test_push_public_key_and_subscription_crud_does_not_return_endpoint_secrets():
    app, _auth, features, principal = setup()
    with TestClient(app) as client:
        key = client.get("/push/vapid-public-key")
        assert key.status_code == 200
        assert key.json() == {"public_key": PUBLIC_KEY, "application_server_key": PUBLIC_KEY}

        created = client.post("/push/subscriptions", json=subscription())
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["endpoint_origin"] == "https://fcm.googleapis.com"
        assert body["id"]
        assert "endpoint" not in body and "p256dh" not in body and "auth" not in body
        assert features.active_push_subscription_count(principal.user_id) == 1

        listed = client.get("/push/subscriptions")
        assert listed.status_code == 200 and listed.json()["total"] == 1
        deleted = client.delete(f"/push/subscriptions/{body['id']}")
        assert deleted.status_code == 204
        assert features.active_push_subscription_count(principal.user_id) == 0
        assert client.delete(f"/push/subscriptions/{body['id']}").status_code == 404


def test_push_subscription_endpoint_validation_blocks_non_https_and_unknown_hosts():
    app, _auth, _features, _principal = setup()
    with TestClient(app) as client:
        bad_scheme = subscription()
        bad_scheme["endpoint"] = bad_scheme["endpoint"].replace("https://", "http://", 1)
        response = client.post("/push/subscriptions", json=bad_scheme)
        assert response.status_code == 422

        bad_host = subscription(host="127.0.0.1")
        response = client.post("/push/subscriptions", json=bad_host)
        assert response.status_code == 422

        bad_keys = subscription()
        bad_keys["keys"]["p256dh"] = "!!!"
        response = client.post("/push/subscriptions", json=bad_keys)
        assert response.status_code == 422


def test_push_subscription_isolated_by_user_and_endpoint_reassignment_prevents_old_owner_delivery():
    app, _auth, features, principal = setup()
    with TestClient(app) as client:
        created = client.post("/push/subscriptions", json=subscription())
        sid = created.json()["id"]

        other = Principal("usr_push_2", {"sub": "usr_push_2", "plan": "premium"}, False)
        app.dependency_overrides[current_principal] = lambda: other
        assert client.delete(f"/push/subscriptions/{sid}").status_code == 404
        reassigned = client.post("/push/subscriptions", json=subscription())
        assert reassigned.status_code == 201
        assert features.active_push_subscription_count(principal.user_id) == 0
        assert features.active_push_subscription_count("usr_push_2") == 1


def test_web_push_preference_can_be_enabled_without_overwriting_other_preferences():
    app, _auth, _features, _principal = setup()
    with TestClient(app) as client:
        response = client.patch("/account/alert-preferences", json={"web_push": True, "telegram": False})
        assert response.status_code == 200
        preferences = response.json()["preferences"]
        assert preferences["web_push"] is True
        assert preferences["telegram"] is False
        assert preferences["liq"] is True


def test_push_subscription_limit_enforced_per_user():
    app, _auth, _features, _principal = setup()
    with TestClient(app) as client:
        for i in range(10):
            response = client.post("/push/subscriptions", json=subscription(str(i)))
            assert response.status_code == 201, response.text
        response = client.post("/push/subscriptions", json=subscription("overflow"))
        assert response.status_code == 409
        assert response.json()["code"] == "PUSH_SUBSCRIPTION_LIMIT"


def test_expired_browser_subscription_is_rejected():
    app, _auth, features, principal = setup()
    expired = subscription("expired")
    expired["expirationTime"] = int((datetime.now(UTC) - timedelta(days=1)).timestamp() * 1000)
    with TestClient(app) as client:
        response = client.post("/push/subscriptions", json=expired)
    assert response.status_code == 422
    assert response.json()["code"] == "PUSH_SUBSCRIPTION_EXPIRED"
    assert features.active_push_subscription_count(principal.user_id) == 0


def test_zero_expiration_timestamp_is_rejected_as_expired():
    app, _auth, features, principal = setup()
    expired = subscription("expired-zero")
    expired["expirationTime"] = 0
    with TestClient(app) as client:
        response = client.post("/push/subscriptions", json=expired)
    assert response.status_code == 422
    assert response.json()["code"] == "PUSH_SUBSCRIPTION_EXPIRED"
    assert features.active_push_subscription_count(principal.user_id) == 0
