from api.mongo_config import mongo_pool_options


def test_mongo_pool_options_use_conservative_defaults(monkeypatch):
    for key in (
        "MONGODB_MAX_POOL_SIZE", "MONGODB_MIN_POOL_SIZE", "MONGODB_MAX_CONNECTING",
        "MONGODB_WAIT_QUEUE_TIMEOUT_MS", "MONGODB_MAX_IDLE_TIME_MS",
    ):
        monkeypatch.delenv(key, raising=False)
    assert mongo_pool_options() == {
        "maxPoolSize": 20,
        "minPoolSize": 0,
        "maxConnecting": 4,
        "waitQueueTimeoutMS": 5000,
        "maxIdleTimeMS": 60000,
    }


def test_mongo_pool_options_clamp_unsafe_or_invalid_values(monkeypatch):
    monkeypatch.setenv("MONGODB_MAX_POOL_SIZE", "10000")
    monkeypatch.setenv("MONGODB_MIN_POOL_SIZE", "500")
    monkeypatch.setenv("MONGODB_MAX_CONNECTING", "-9")
    monkeypatch.setenv("MONGODB_WAIT_QUEUE_TIMEOUT_MS", "12")
    monkeypatch.setenv("MONGODB_MAX_IDLE_TIME_MS", "bad")
    assert mongo_pool_options() == {
        "maxPoolSize": 100,
        "minPoolSize": 10,
        "maxConnecting": 1,
        "waitQueueTimeoutMS": 1000,
        "maxIdleTimeMS": 60000,
    }
