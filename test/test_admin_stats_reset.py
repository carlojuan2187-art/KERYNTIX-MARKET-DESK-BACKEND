from datetime import datetime, timedelta, timezone

from api.repositories.business import InMemoryBusinessRepository
from api.repositories.signals import InMemorySignalRepository
from api.services import stats_baseline as baseline


def _row(signal_id: str, closed_at: datetime) -> dict:
    return {"signal_id": signal_id, "id": signal_id, "result": "TP1", "r_real": 1.0, "closed_at": closed_at}


def test_reset_hides_old_trades_without_deleting_them_and_can_be_restored():
    now = datetime.now(timezone.utc)
    signals = InMemorySignalRepository([], [_row("old", now - timedelta(days=3)), _row("older", now - timedelta(days=5))])
    business = InMemoryBusinessRepository()

    assert baseline.get_reset_at(business) is None
    assert baseline.describe(business, signals)["trades_counted"] == 2

    reset_at = baseline.apply_reset(business, actor_id="admin-1", reason="Nueva estrategia", now=now - timedelta(days=1))
    signals._results.append(_row("new", now))

    info = baseline.describe(business, signals)
    assert info["active"] is True
    assert info["trades_counted"] == 1 and info["trades_archived"] == 2 and info["trades_total"] == 3
    kept = baseline.rows_since(signals.list_results(limit=100), baseline.get_reset_at(business))
    assert [row["signal_id"] for row in kept] == ["new"]
    assert baseline.latest_start(now - timedelta(days=30), reset_at) == reset_at
    assert baseline.latest_start(None, reset_at) == reset_at

    baseline.clear_reset(business)
    assert baseline.get_reset_at(business) is None
    assert baseline.describe(business, signals)["trades_counted"] == 3
