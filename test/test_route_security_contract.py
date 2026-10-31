from __future__ import annotations

from fastapi.routing import APIRoute

from api.routes.admin import require_admin
from api.security import current_principal, require_analytical_access
from main import create_app


PUBLIC_ROUTES = {
    ("GET", "/"),
    ("GET", "/live"),
    ("GET", "/health"),
    ("GET", "/ready"),
    ("POST", "/auth/phone"),
    ("POST", "/auth/register"),
    ("POST", "/auth/recover"),
    ("POST", "/auth/login"),
    ("GET", "/auth/telegram-check"),
    ("POST", "/auth/verify"),
    ("POST", "/auth/resend"),
    ("POST", "/auth/refresh"),
    ("POST", "/auth/logout"),
    ("GET", "/billing/plans"),
    ("GET", "/push/vapid-public-key"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
}


def _dependency_calls(dependant):
    if dependant.call is not None:
        yield dependant.call
    for child in dependant.dependencies:
        yield from _dependency_calls(child)


def test_all_non_public_business_routes_have_authentication_dependencies():
    app = create_app()
    missing: list[str] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = set(_dependency_calls(route.dependant))
        for method in route.methods or set():
            if method in {"HEAD", "OPTIONS"}:
                continue
            if (method, route.path) in PUBLIC_ROUTES:
                continue
            if route.path.startswith("/admin/") or route.path == "/admin":
                if require_admin not in calls:
                    missing.append(f"{method} {route.path}: require_admin")
            elif not ({current_principal, require_analytical_access, require_admin} & calls):
                missing.append(f"{method} {route.path}: authentication")
    assert missing == [], "Rutas de negocio sin guardas: " + ", ".join(missing)
