from datetime import datetime, timedelta, timezone

from api.repositories.sweeps import InMemorySweepStore


def test_sweep_store_keeps_newest_and_expires_old_observations():
    now = datetime.now(timezone.utc)
    store = InMemorySweepStore()
    first = {"_id": "BTCUSDT:plus:LONG", "event_at": now, "expires_at": now + timedelta(minutes=20), "level": 100}
    newer = {**first, "event_at": now + timedelta(seconds=1), "level": 101}
    store.save_observations([first])
    store.save_observations([newer])
    current = store.load_active(now=now)
    assert current[first["_id"]]["level"] == 101
    assert store.load_active(now=now + timedelta(minutes=30)) == {}
