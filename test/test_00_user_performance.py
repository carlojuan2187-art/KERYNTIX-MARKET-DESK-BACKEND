from datetime import datetime, timezone

from api.repositories.signals import InMemorySignalRepository
from api.services.user_performance import personal_result_rows


def at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_personal_results_only_include_signals_delivered_to_that_user():
    repository = InMemorySignalRepository(results=[
        {"id": "one", "signal_id": "one", "result": "TP1", "closed_at": at("2026-01-02T12:00:00Z")},
        {"id": "two", "signal_id": "two", "result": "SL", "closed_at": at("2026-01-02T13:00:00Z")},
    ])
    repository.record_user_deliveries("alice", ["one", "one"], delivered_at=at("2026-01-02T10:00:00Z"))
    repository.record_user_deliveries("bob", ["two"], delivered_at=at("2026-01-02T10:00:00Z"))

    claims = {
        "created_at": at("2026-01-01T00:00:00Z"),
        "trial_end": at("2026-01-10T00:00:00Z"),
    }
    alice_rows = personal_result_rows(repository, user_id="alice", claims=claims)
    bob_rows = personal_result_rows(repository, user_id="bob", claims=claims)

    assert [row["signal_id"] for row in alice_rows] == ["one"]
    assert [row["signal_id"] for row in bob_rows] == ["two"]


def test_plan_expiry_freezes_results_during_access_gap_and_resumes_after_renewal():
    repository = InMemorySignalRepository(results=[
        {"id": "gap-one", "signal_id": "gap-one", "result": "SL", "closed_at": at("2026-01-04T12:00:00Z")},
        {"id": "active-one", "signal_id": "active-one", "result": "TP1", "closed_at": at("2026-01-06T12:00:00Z")},
        {"id": "gap-two", "signal_id": "gap-two", "result": "SL", "closed_at": at("2026-01-08T12:00:00Z")},
        {"id": "renewed-one", "signal_id": "renewed-one", "result": "TP2", "closed_at": at("2026-01-10T12:00:00Z")},
    ])
    repository.record_user_deliveries(
        "alice", ["gap-one", "active-one", "gap-two", "renewed-one"],
        delivered_at=at("2026-01-02T10:00:00Z"),
    )
    claims = {
        "created_at": at("2026-01-01T00:00:00Z"),
        "trial_end": at("2026-01-03T00:00:00Z"),
        "plan_history": [
            {"effective_start": at("2026-01-05T00:00:00Z"), "effective_end": at("2026-01-07T00:00:00Z")},
            {"effective_start": at("2026-01-09T00:00:00Z"), "effective_end": at("2026-01-12T00:00:00Z")},
        ],
    }

    rows = personal_result_rows(repository, user_id="alice", claims=claims)
    assert [row["signal_id"] for row in rows] == ["active-one", "renewed-one"]


def test_global_reset_archives_old_personal_deliveries_and_restore_can_read_them_again():
    repository = InMemorySignalRepository(results=[
        {"id": "before", "signal_id": "before", "result": "TP1", "closed_at": at("2026-01-06T12:00:00Z")},
        {"id": "after", "signal_id": "after", "result": "SL", "closed_at": at("2026-01-07T12:00:00Z")},
    ])
    repository.record_user_deliveries("alice", ["before"], delivered_at=at("2026-01-05T10:00:00Z"))
    repository.record_user_deliveries("alice", ["after"], delivered_at=at("2026-01-06T10:00:00Z"))
    claims = {
        "created_at": at("2026-01-01T00:00:00Z"),
        "trial_end": at("2026-01-20T00:00:00Z"),
    }

    current = personal_result_rows(
        repository, user_id="alice", claims=claims, reset_at=at("2026-01-06T00:00:00Z")
    )
    restored = personal_result_rows(repository, user_id="alice", claims=claims)

    assert [row["signal_id"] for row in current] == ["after"]
    assert {row["signal_id"] for row in restored} == {"before", "after"}



def test_closed_signal_snapshot_recovers_missing_result_projection_for_personal_history():
    now = at("2026-01-06T12:00:00Z")
    signal = {
        "id": "closed-without-projection",
        "symbol": "BTCUSDT",
        "direction": "LONG",
        "visibility": "premium",
        "entry_price": 100.0,
        "entry_zone": {"low": 99.5, "high": 100.5},
        "stop_loss": 98.0,
        "take_profits": [104.0, 106.0],
        "created_at": at("2026-01-06T10:00:00Z"),
        "touched_at": at("2026-01-06T10:05:00Z"),
        "status": "TOUCHED",
        "outcome": {
            "state": "CLOSED",
            "result": "TP2",
            "r_real": 3.0,
            "closed_at": now,
            "tp1_at": at("2026-01-06T11:00:00Z"),
            "tp2_at": now,
            "exit_price": 106.0,
            "exit_trigger_price": 106.0,
            "exit_via": "TP2",
            "calculation_version": "2.3",
            "execution_model": "SIMULATED_FULL_POSITION_TP1_CREDIT_ENTRY_PROTECTION",
        },
    }
    repository = InMemorySignalRepository([signal], results=[])
    repository.record_user_deliveries(
        "alice", ["closed-without-projection"],
        delivered_at=at("2026-01-06T10:01:00Z"),
    )
    claims = {
        "created_at": at("2026-01-01T00:00:00Z"),
        "trial_end": at("2026-01-10T00:00:00Z"),
    }

    rows = personal_result_rows(repository, user_id="alice", claims=claims)

    assert len(rows) == 1
    assert rows[0]["signal_id"] == "closed-without-projection"
    assert rows[0]["result"] == "TP2"
    assert rows[0]["r_real"] == 3.0
