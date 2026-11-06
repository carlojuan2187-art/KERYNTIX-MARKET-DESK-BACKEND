"""Offline release preflight for KERYNTIX MARKET DESK Web.

This command checks code/deployment readiness without connecting to MongoDB,
Redis, Binance, BSC RPC or Telegram. It is intentionally safe to run in CI and
before creating Railway services; it only reports configuration issue codes,
never secret values.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

# Core endpoints that the React client and operational workflows depend on.
REQUIRED_HTTP_ROUTES: set[tuple[str, str]] = {
    ("GET", "/live"), ("GET", "/health"), ("GET", "/ready"),
    ("POST", "/auth/phone"), ("POST", "/auth/verify"), ("POST", "/auth/refresh"),
    ("POST", "/auth/logout"), ("GET", "/auth/me"),
    ("GET", "/account/plan"), ("GET", "/account/entitlements"),
    ("GET", "/signals"), ("GET", "/signals/results"), ("GET", "/market/quotes"),
    ("GET", "/market/candles"), ("GET", "/symbols"), ("GET", "/symbols/{symbol}"),
    ("GET", "/overview"), ("GET", "/radar"), ("GET", "/results"), ("POST", "/calculator"),
    ("GET", "/watchlist"), ("POST", "/watchlist"),
    ("GET", "/notifications"), ("POST", "/notifications/read-all"),
    ("GET", "/push/vapid-public-key"), ("POST", "/push/subscriptions"),
    ("GET", "/billing/plans"), ("POST", "/billing/payments"),
    ("POST", "/billing/payments/{payment_id}/verify"),
    ("GET", "/referrals/summary"), ("POST", "/support/tickets"),
    ("GET", "/admin/summary"), ("GET", "/admin/users"),
    ("POST", "/admin/users/{user_id}/premium"), ("POST", "/admin/users/{user_id}/ban"),
    ("POST", "/admin/users/{user_id}/unban"), ("GET", "/admin/audit"),
    ("POST", "/events/ticket"),
}
REQUIRED_WEBSOCKET_ROUTES = {"/ws/events"}

# Eight workers are supervised together; the Telegram login/pairing bot is a
# ninth, separately deployable process. Names here must match status keys used
# by /health and DistributedWorkerLease.
MODULE_STATUS_NAMES = {
    "worker.market_feed": "market-feed",
    "worker.signal_scanner": "signal-scanner",
    "worker.signal_lifecycle": "signal-lifecycle",
    "worker.plan_scheduler": "plan-scheduler",
    "worker.user_alerts": "user-alert-dispatcher",
    "worker.business_reconciler": "business-reconciler",
    "worker.telegram_notification_delivery": "telegram-notification-delivery",
    "worker.web_push_delivery": "web-push-delivery",
    "worker.telegram_auth_bot": "telegram-auth-bot",
}
REQUIRED_FILES = (
    "main.py", "requirements.txt", ".env.example", "api/routes/auth.py",
    "api/routes/billing.py", "api/routes/admin.py", "api/routes/events.py",
    "api/services/plans.py", "api/services/payments.py",
    "api/services/business_notifications.py", "worker/supervisor.py",
    "worker/telegram_auth_bot.py", "worker/telegram_notification_delivery.py",
    "worker/web_push_delivery.py", "API_CONTRACT.md", "Dockerfile.worker", "WORKER-RAILWAY-DEPLOY.md",
)
CORE_IMPORTS = ("fastapi", "pydantic", "pymongo", "redis", "httpx", "requests", "pandas", "websockets")
PRODUCTION_IMPORTS = ("cryptography", "pywebpush")


def route_inventory(app: Any) -> tuple[set[tuple[str, str]], set[str]]:
    """Return effective HTTP paths and WebSocket paths for the app.

    FastAPI 0.137+ keeps included routers as a lazy route tree instead of
    flattening them into app.routes. OpenAPI resolves that tree into the
    effective HTTP contract, while WebSocket routes (not represented in
    OpenAPI) are discovered by walking the included routers recursively.
    """
    http: set[tuple[str, str]] = set()
    websockets: set[str] = set()

    openapi = getattr(app, "openapi", None)
    if callable(openapi):
        schema = openapi()
        for path, operations in (schema.get("paths") or {}).items():
            if not isinstance(path, str) or not isinstance(operations, dict):
                continue
            for method in operations:
                verb = str(method).upper()
                if verb in {"GET", "POST", "PUT", "PATCH", "DELETE", "TRACE"}:
                    http.add((verb, path))

    seen: set[int] = set()

    def collect_websockets(routes: Iterable[Any]) -> None:
        for route in routes:
            marker = id(route)
            if marker in seen:
                continue
            seen.add(marker)

            route_type = type(route).__name__.lower()
            path = getattr(route, "path", None)
            if isinstance(path, str) and (
                route_type in {"apiwebsocketroute", "websocketroute"}
                or ("websocket" in route_type and hasattr(route, "endpoint"))
            ):
                websockets.add(path)

            # FastAPI 0.137+ represents included routers as _IncludedRouter
            # nodes that reference their source APIRouter. Older versions expose
            # child routes directly in app.routes.
            original_router = getattr(route, "original_router", None)
            nested_routes = getattr(original_router, "routes", None)
            if nested_routes is not None:
                collect_websockets(nested_routes)

    collect_websockets(getattr(app, "routes", ()))
    return http, websockets

def _compile_source_tree(root: Path) -> list[str]:
    issues: list[str] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        except Exception as exc:
            issues.append(f"PYTHON_SOURCE_INVALID:{path.relative_to(root)}:{type(exc).__name__}")
    return issues


def inspect_backend(
    *, root: Path | None = None,
    app: Any | None = None,
    production: bool | None = None,
    python_version: tuple[int, int, int] | None = None,
    module_finder=importlib.util.find_spec,
    production_issue_provider=None,
) -> dict[str, Any]:
    root = (root or Path(__file__).resolve().parents[1]).resolve()
    production = (os.getenv("FTRADE_ENV", "development").strip().lower() == "production") if production is None else bool(production)
    version = python_version or sys.version_info[:3]
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    file_issues = [relative for relative in REQUIRED_FILES if not (root / relative).resolve().is_file()]
    add("required-files", "error" if file_issues else "ok",
        "Missing: " + ", ".join(file_issues) if file_issues else "Required backend, contract, and configuration files exist.")

    compile_issues = _compile_source_tree(root)
    add("python-source-compilation", "error" if compile_issues else "ok",
        "; ".join(compile_issues[:12]) if compile_issues else "All Python sources compile without executing network calls.")

    app_import_issue = None
    if app is None:
        # Import app only after root is added; creation is lazy and does not fetch
        # external data. Catch dependency/config failures so the preflight can
        # still report all other checks rather than exiting with a traceback.
        root_string = str(root)
        if root_string not in sys.path:
            sys.path.insert(0, root_string)
        try:
            from main import app as default_app  # type: ignore
            app = default_app
        except Exception as exc:
            app_import_issue = f"APP_IMPORT_FAILED:{type(exc).__name__}"
    if app is None:
        route_detail = [app_import_issue or "APP_IMPORT_FAILED"]
    else:
        http_routes, websocket_routes = route_inventory(app)
        missing_http = sorted(REQUIRED_HTTP_ROUTES - http_routes)
        missing_ws = sorted(REQUIRED_WEBSOCKET_ROUTES - websocket_routes)
        route_detail = []
        if missing_http:
            route_detail.append("HTTP missing: " + ", ".join(f"{m} {p}" for m, p in missing_http))
        if missing_ws:
            route_detail.append("WebSocket missing: " + ", ".join(missing_ws))
    add("api-contract-routes", "error" if route_detail else "ok",
        "; ".join(route_detail) if route_detail else f"{len(REQUIRED_HTTP_ROUTES)} required HTTP routes and {len(REQUIRED_WEBSOCKET_ROUTES)} WebSocket routes are registered.")

    try:
        if root not in (Path(__file__).resolve().parents[1],):
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
        from worker.supervisor import WORKER_MODULES
        from main import PRODUCTION_REQUIRED_WORKERS
        supervised = set(WORKER_MODULES)
        worker_file_issues: list[str] = []
        entrypoint_issues: list[str] = []
        for module in supervised | {"worker.telegram_auth_bot"}:
            module_path = root / (module.replace(".", "/") + ".py")
            if not module_path.is_file():
                worker_file_issues.append(str(module_path.relative_to(root)))
                continue
            source = module_path.read_text(encoding="utf-8")
            if 'if __name__ == "__main__"' not in source and "if __name__ == '__main__'" not in source:
                entrypoint_issues.append(module)
        expected_statuses = set(MODULE_STATUS_NAMES.values())
        try:
            from main import PRODUCTION_REQUIRED_WORKERS
            configured_statuses = set(PRODUCTION_REQUIRED_WORKERS)
        except Exception:
            configured_statuses = set()
        status_ok = expected_statuses == configured_statuses
        supervisor_ok = set(MODULE_STATUS_NAMES).issubset(supervised | {"worker.telegram_auth_bot"}) and "worker.telegram_auth_bot" not in supervised
        worker_ok = status_ok and supervisor_ok and not worker_file_issues and not entrypoint_issues
        worker_detail = "Eight background workers are supervised; Telegram auth is a separate process; all nine health identities and runnable entrypoints are aligned."
        if not worker_ok:
            worker_detail = (f"status-mismatch={sorted(expected_statuses ^ configured_statuses)}; "
                             f"supervisor-mismatch={sorted(set(MODULE_STATUS_NAMES) - supervised - {'worker.telegram_auth_bot'})}; "
                             f"missing-files={worker_file_issues}; missing-entrypoints={entrypoint_issues}")
        add("worker-health-contract", "ok" if worker_ok else "error", worker_detail)
    except Exception as exc:
        add("worker-health-contract", "error", f"Could not inspect worker inventory ({type(exc).__name__}).")

    worker_dockerfile = root / "Dockerfile.worker"
    deploy_guide = root / "WORKER-RAILWAY-DEPLOY.md"
    try:
        dockerfile_text = worker_dockerfile.read_text(encoding="utf-8")
        deployment_contract_ok = (
            'CMD ["python", "-m", "worker.supervisor"]' in dockerfile_text
            and "REDIS_URL" in deploy_guide.read_text(encoding="utf-8")
            and "FTRADE_DEPLOYMENT_NAME" in deploy_guide.read_text(encoding="utf-8")
        )
        add("worker-deployment-contract", "ok" if deployment_contract_ok else "error",
            "Dedicated worker image and Railway instructions agree on the supervised worker entrypoint and shared Redis namespace."
            if deployment_contract_ok else
            "Dockerfile.worker must start worker.supervisor and the deployment guide must document shared Redis and worker namespace.")
    except OSError as exc:
        add("worker-deployment-contract", "error", f"Could not read worker deployment files ({type(exc).__name__}).")

    missing_core = []
    for module_name in CORE_IMPORTS:
        try:
            available = module_finder(module_name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            available = False
        if not available:
            missing_core.append(module_name)
    dependency_status = "error" if (missing_core and production) else ("warning" if missing_core else "ok")
    add("core-runtime-dependencies", dependency_status,
        "Missing: " + ", ".join(missing_core) if missing_core else "Core imports are available in this interpreter.")

    missing_prod_imports = []
    if production:
        for module_name in PRODUCTION_IMPORTS:
            try:
                available = module_finder(module_name) is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                available = False
            if not available:
                missing_prod_imports.append(module_name)
    add("production-only-dependencies", "error" if missing_prod_imports else ("ok" if production else "warning"),
        "Missing: " + ", ".join(missing_prod_imports) if missing_prod_imports else ("Required cryptography and Web Push packages are available." if production else "Not enforced for local development; production requires cryptography and pywebpush."))

    version_status = "ok" if tuple(version[:2]) == (3, 11) else ("error" if production else "warning")
    add("python-target-version", version_status,
        f"Current interpreter is {version[0]}.{version[1]}.{version[2]}; deployment target is Python 3.11.")

    if production:
        if production_issue_provider is None:
            try:
                if str(root) not in sys.path:
                    sys.path.insert(0, str(root))
                from main import _production_configuration_issues
                production_issue_provider = _production_configuration_issues
            except Exception as exc:
                production_issue_provider = lambda: [f"PRODUCTION_CONFIG_CHECK_FAILED:{type(exc).__name__}"]
        try:
            config_issues = list(production_issue_provider())
        except Exception as exc:
            config_issues = [f"PRODUCTION_CONFIG_CHECK_FAILED:{type(exc).__name__}"]
        add("production-configuration", "error" if config_issues else "ok",
            ", ".join(config_issues) if config_issues else "Required production configuration passes static validation.")
    else:
        add("production-configuration", "warning", "Skipped: use --production to check required production configuration variables and VAPID key pairing.")

    errors = [item for item in checks if item["status"] == "error"]
    warnings = [item for item in checks if item["status"] == "warning"]
    return {
        "status": "failed" if errors else "passed",
        "mode": "production" if production else "development",
        "python": f"{version[0]}.{version[1]}.{version[2]}",
        "check_count": len(checks),
        "error_count": len(errors),
        "warning_count": len(warnings),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight estático de liberación de KERYNTIX MARKET DESK Web (no conecta a servicios externos).")
    parser.add_argument("--production", action="store_true", help="Validar variables y dependencias requeridas para producción.")
    parser.add_argument("--json", action="store_true", help="Imprimir el reporte en JSON.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help="Carpeta backend/ a inspeccionar.")
    args = parser.parse_args()
    if args.production:
        os.environ["FTRADE_ENV"] = "production"
    report = inspect_backend(root=args.root, production=args.production or os.getenv("FTRADE_ENV", "development").lower() == "production")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"KERYNTIX MARKET DESK release preflight — {report['mode']} — Python {report['python']}")
        for check in report["checks"]:
            print(f"[{check['status'].upper():7}] {check['name']}: {check['detail']}")
        print(f"Resultado: {report['status']} ({report['error_count']} errores, {report['warning_count']} advertencias)")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
