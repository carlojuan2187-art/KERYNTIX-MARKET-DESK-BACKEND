from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api.repositories.signals import InMemorySignalRepository
from api.security import Principal, current_principal, require_analytical_access
from engine.performance import build_performance, period_start
from main import create_app


def _result(idx, result, r_real, closed_at):
    exit_levels = {"stop_loss": 98.0, "take_profits": [102.0, 104.0]}
    return {
        "id": f"signal-{idx}", "signal_id": f"signal-{idx}", "symbol": "BTCUSDT",
        "direction": "LONG", "entry_fill": 100.0, "result": result,
        "r_real": r_real, "closed_at": closed_at,
        "touched_at": closed_at - timedelta(minutes=4), **exit_levels,
    }


def test_performance_counts_only_closed_results_and_builds_curve():
    now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    rows = [
        _result(1, "TP1", 1.0, now - timedelta(hours=3)),
        _result(2, "SL", -1.0, now - timedelta(hours=2)),
        _result(3, "TP2", 2.0, now - timedelta(hours=1)),
        _result(4, "INVALID", 0.0, now),
    ]
    result = build_performance(rows, period="all", now=now)
    assert result["operations"] == 3
    assert result["wins"] == 2
    assert result["losses"] == 1
    assert result["win_rate"] == round(2 / 3 * 100, 2)
    assert result["total_r"] == 2.0
    assert result["average_r"] == round(round(2.0, 2) / 3, 2)
    assert result["streak"] == 1
    assert result["curve"][-1]["r"] == 2.0
    assert result["trades"][0]["id"] == "signal-3"  # newest first for the table
    assert result["trades"][0]["exit_price"] == 104.0
    assert result["trades"][-1]["exit_price"] == 102.0


def test_performance_period_uses_utc_calendar_boundaries_like_bot():
    now = datetime(2026, 10, 25, 12, 34, tzinfo=timezone.utc)  # Sunday
    assert period_start("day", now=now) == datetime(2026, 10, 25, tzinfo=timezone.utc)
    assert period_start("week", now=now) == datetime(2026, 10, 19, tzinfo=timezone.utc)
    assert period_start("month", now=now) == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert period_start("all", now=now) is None


def test_performance_route_returns_actual_snapshot_values():
    now = datetime.now(timezone.utc)
    repo = InMemorySignalRepository([], [
        _result(1, "TP1", 1.25, now - timedelta(minutes=5)),
        _result(2, "SL", -1.0, now - timedelta(minutes=2)),
    ])
    app = create_app(repo)
    principal = Principal("user-1", {"sub": "user-1", "plan": "premium", "plan_end": "2099-01-01T00:00:00Z"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as client:
        response = client.get("/results?period=all")
    assert response.status_code == 200
    data = response.json()
    assert data["operations"] == 2
    assert data["total_r"] == 0.25
    assert data["trades"][0]["result"] == "SL"
    assert data["trades"][0]["exit_price"] == 98.0


def test_performance_route_rejects_unknown_period():
    app = create_app(InMemorySignalRepository())
    principal = Principal("user-1", {"sub": "user-1", "plan": "premium", "plan_end": "2099-01-01T00:00:00Z"}, False)
    app.dependency_overrides[current_principal] = lambda: principal
    app.dependency_overrides[require_analytical_access] = lambda: principal
    with TestClient(app) as client:
        response = client.get("/results?period=year")
    assert response.status_code == 422


def test_performance_requires_analytical_authentication():
    app = create_app(InMemorySignalRepository())
    with TestClient(app) as client:
        response = client.get("/results?period=all")
    assert response.status_code == 401


def test_all_time_stats_do_not_use_the_500_row_api_listing_cap():
    now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    rows = [_result(i, "TP1", 1.0, now - timedelta(minutes=i)) for i in range(650)]
    result = build_performance(rows, period="all", now=now)
    assert result["operations"] == 650


def test_performance_prefers_explicit_simulated_exit_and_counts_legacy_snapshots():
    now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    current = _result(1, "TP1", 2.0, now - timedelta(minutes=2))
    current.update({
        "exit_price": 102.0,
        "calculation_version": "2.0",
        "execution_model": "SIMULATED_FULL_POSITION_TP1_PROTECTION",
        "entry_reference": "entry_price",
        "exit_via": "TP1_PROTECTIVE_STOP_LIVE_PRICE",
    })
    legacy = _result(2, "SL", -1.0, now - timedelta(minutes=1))

    result = build_performance([current, legacy], period="all", now=now)

    trade_by_id = {trade["id"]: trade for trade in result["trades"]}
    assert trade_by_id["signal-1"]["exit_price"] == 102.0
    assert trade_by_id["signal-1"]["calculation_version"] == "2.0"
    assert trade_by_id["signal-1"]["exit_via"] == "TP1_PROTECTIVE_STOP_LIVE_PRICE"
    assert trade_by_id["signal-2"]["calculation_version"] == "legacy_v1"
    assert result["legacy_results"] == 1
    assert result["calculation_version"] == "2.0"


def test_performance_response_preserves_ambiguous_candle_resolution_flag():
    now = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)
    row = _result(1, "TP1", 2.0, now - timedelta(minutes=1))
    row.update({
        "exit_price": 102.0, "calculation_version": "2.0",
        "execution_model": "SIMULATED_FULL_POSITION_TP1_PROTECTION",
        "exit_via": "TP1_TP2_SAME_CANDLE_PROTECTIVE_AMBIGUOUS",
        "ambiguous": True,
    })
    result = build_performance([row], period="all", now=now)
    assert result["trades"][0]["ambiguous"] is True
