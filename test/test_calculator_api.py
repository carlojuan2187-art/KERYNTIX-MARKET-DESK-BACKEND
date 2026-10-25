from datetime import datetime, timezone

from fastapi.testclient import TestClient

from api.repositories.signals import InMemorySignalRepository
from api.security import Principal, current_principal, require_analytical_access
from main import create_app


def _client():
    now = datetime.now(timezone.utc)
    app = create_app(InMemorySignalRepository())
    principal = Principal("user-1", {"sub": "user-1", "plan": "premium", "plan_end": "2099-01-01T00:00:00Z"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    return TestClient(app)


def test_calculator_maps_original_bot_rules_to_web_contract():
    with _client() as client:
        response = client.post("/calculator", json={
            "capital": 1000, "risk_pct": 1, "entry": 100,
            "stop": 99, "tp1": 102, "tp2": 103,
        })
    assert response.status_code == 200
    result = response.json()
    assert result["direction"] == "LONG"
    assert result["risk_usdt"] == 10
    assert result["qty"] == 10
    assert result["position_size"] == 1000
    assert result["leverage_min"] == 1
    assert result["fee_taker_pct"] == 0.05
    assert result["results"][0]["level"] == "TP1"
    assert result["results"][1]["level"] == "TP2"
    assert result["risk_feasible"] is True


def test_calculator_short_direction_inferred_and_fees_follow_bot_model():
    with _client() as client:
        response = client.post("/calculator", json={
            "capital": 500, "risk_pct": 2, "entry": 100,
            "stop": 102, "tp1": 97,
        })
    assert response.status_code == 200
    result = response.json()
    assert result["direction"] == "SHORT"
    assert result["qty"] == 5
    assert result["results"][0]["usdt"] == 15
    # Source bot estimates round-trip fee from entry notional, per its original rule.
    assert result["results"][0]["fees_usdt"] == result["fees_usdt"]


def test_calculator_rejects_risk_above_original_bot_limit():
    with _client() as client:
        response = client.post("/calculator", json={
            "capital": 1000, "risk_pct": 10.1, "entry": 100,
            "stop": 99, "tp1": 102,
        })
    assert response.status_code == 422


def test_calculator_rejects_invalid_geometry_with_stable_error():
    with _client() as client:
        response = client.post("/calculator", json={
            "capital": 1000, "risk_pct": 1, "entry": 100,
            "stop": 99, "tp1": 98,
        })
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_RISK_INPUT"


def test_calculator_rejects_tp3_without_tp2():
    with _client() as client:
        response = client.post("/calculator", json={
            "capital": 1000, "risk_pct": 1, "entry": 100,
            "stop": 99, "tp1": 102, "tp3": 104,
        })
    assert response.status_code == 422


def test_calculator_requires_analytical_authentication():
    app = create_app(InMemorySignalRepository())
    with TestClient(app) as client:
        response = client.post("/calculator", json={
            "capital": 1000, "risk_pct": 1, "entry": 100, "stop": 99, "tp1": 102,
        })
    assert response.status_code == 401
