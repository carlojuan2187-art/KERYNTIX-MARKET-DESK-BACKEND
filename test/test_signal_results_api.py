from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api.repositories.signals import InMemorySignalRepository
from api.security import Principal, current_principal, require_analytical_access
from main import create_app


def test_results_endpoint_returns_closed_trade_snapshots():
    now = datetime.now(timezone.utc)
    result = {
        "id": "signal-1", "signal_id": "signal-1", "schema_version": "1.0",
        "symbol": "BTCUSDT", "direction": "LONG", "visibility": "premium",
        "entry_fill": 101.0, "entry_zone": {"low": 100.0, "high": 101.0},
        "stop_loss": 98.0, "take_profits": [103.0, 105.0], "risk": 3.0,
        "r_tp1": 0.6667, "r_tp2": 1.3333, "result": "TP2", "outcome": "win",
        "r_real": 1.3333, "ambiguous": False, "score": 87.0,
        "created_at": now - timedelta(minutes=15), "touched_at": now - timedelta(minutes=10),
        "tp1_at": now - timedelta(minutes=5), "closed_at": now, "evaluated_at": now,
        "duration_minutes": 10.0,
    }
    repository = InMemorySignalRepository([], [result])
    repository.record_user_deliveries(
        "user-42", ["signal-1"], delivered_at=now - timedelta(minutes=10),
    )
    app = create_app(repository)
    principal = Principal("user-42", {
        "sub": "user-42", "plan": "premium",
        "created_at": now - timedelta(days=30),
        "plan_updated_at": now - timedelta(days=30),
        "plan_end": (now + timedelta(days=1)).isoformat(),
    }, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as client:
        response = client.get("/signals/results")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["signal_id"] == "signal-1"
    assert data[0]["result"] == "TP2"
    assert data[0]["r_real"] == 1.3333
