from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from scripts.release_preflight import (
    MODULE_STATUS_NAMES,
    REQUIRED_HTTP_ROUTES,
    REQUIRED_WEBSOCKET_ROUTES,
    inspect_backend,
    route_inventory,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_release_preflight_passes_route_worker_and_source_inventory(monkeypatch):
    monkeypatch.setenv("FTRADE_ENV", "development")
    from main import app

    report = inspect_backend(root=BACKEND_ROOT, app=app, production=False, python_version=(3, 11, 9))

    assert report["status"] == "passed", report
    assert report["error_count"] == 0
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["api-contract-routes"]["status"] == "ok"
    assert checks["worker-health-contract"]["status"] == "ok"
    assert checks["python-target-version"]["status"] == "ok"


def test_route_inventory_reports_a_missing_contract_route():
    class APIWebSocketRoute:
        def __init__(self, path):
            self.path = path
            self.name = "ws-events"
            self.endpoint = object()

    fake_app = SimpleNamespace(routes=[
        SimpleNamespace(path="/live", methods={"GET"}),
        APIWebSocketRoute("/ws/events"),
    ])
    http, websockets = route_inventory(fake_app)

    assert ("GET", "/live") in http
    assert "/ws/events" in websockets
    assert REQUIRED_HTTP_ROUTES - http
    assert REQUIRED_WEBSOCKET_ROUTES <= websockets


def test_preflight_requires_all_declared_worker_health_identities():
    from main import PRODUCTION_REQUIRED_WORKERS
    from worker.supervisor import WORKER_MODULES

    assert set(PRODUCTION_REQUIRED_WORKERS) == set(MODULE_STATUS_NAMES.values())
    assert set(MODULE_STATUS_NAMES) == set(WORKER_MODULES) | {"worker.telegram_auth_bot"}
    assert "worker.telegram_auth_bot" not in WORKER_MODULES


def test_production_preflight_fails_closed_on_missing_config_codes_without_secret_values(monkeypatch):
    monkeypatch.setenv("FTRADE_ENV", "production")
    from main import app

    def no_missing_modules(name):
        return object()

    report = inspect_backend(
        root=BACKEND_ROOT,
        app=app,
        production=True,
        python_version=(3, 11, 9),
        module_finder=no_missing_modules,
        production_issue_provider=lambda: ["ACCESS_TOKEN_SECRET_MISSING_OR_WEAK", "WEB_PUSH_VAPID_NOT_CONFIGURED"],
    )
    assert report["status"] == "failed"
    config = next(item for item in report["checks"] if item["name"] == "production-configuration")
    assert config["status"] == "error"
    assert "ACCESS_TOKEN_SECRET_MISSING_OR_WEAK" in config["detail"]
    assert "replace-with" not in str(report)


def test_production_preflight_rejects_wrong_python_target(monkeypatch):
    monkeypatch.setenv("FTRADE_ENV", "production")
    from main import app

    report = inspect_backend(
        root=BACKEND_ROOT,
        app=app,
        production=True,
        python_version=(3, 13, 5),
        module_finder=lambda _name: object(),
        production_issue_provider=lambda: [],
    )
    target = next(item for item in report["checks"] if item["name"] == "python-target-version")
    assert target["status"] == "error"
    assert report["status"] == "failed"


def test_preflight_never_reports_secret_environment_values(monkeypatch):
    monkeypatch.setenv("ACCESS_TOKEN_SECRET", "this-is-a-test-secret-that-must-not-appear")
    monkeypatch.setenv("AUTH_CODE_PEPPER", "another-test-secret-that-must-not-appear")
    from main import app

    report = inspect_backend(
        root=BACKEND_ROOT,
        app=app,
        production=True,
        python_version=(3, 11, 9),
        module_finder=lambda _name: object(),
        production_issue_provider=lambda: ["ADMIN_USER_IDS_EMPTY"],
    )
    assert "this-is-a-test-secret-that-must-not-appear" not in str(report)
    assert "another-test-secret-that-must-not-appear" not in str(report)
