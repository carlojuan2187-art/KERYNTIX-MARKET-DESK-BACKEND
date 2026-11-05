"""KERYNTIX MARKET DESK API entrypoint: signals, market data, authentication and account sessions."""
from __future__ import annotations

from api.logging_config import configure_logging

import base64
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # Environment may be injected by the process manager.
    pass
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from api.cache import JsonCache, cache_from_environment
from api.repositories.auth import AuthRepository, InMemoryAuthRepository, auth_repository_from_environment
from api.repositories.business import business_repository_from_environment
from api.repositories.signals import InMemorySignalRepository, SignalRepository, repository_from_environment
from api.repositories.sweeps import InMemorySweepStore, SweepStore, sweep_store_from_environment
from api.repositories.user_features import (InMemoryUserFeatureRepository, UserFeatureRepository, user_feature_repository_from_environment)
from api.routes.account import router as account_router
from api.routes.admin import router as admin_router
from api.routes.billing import router as billing_router
from api.routes.compat import router as compatibility_router
from api.routes.events import router as events_router
from api.routes.symbols import router as symbols_router
from api.routes.referrals import router as referrals_router
from api.routes.support import router as support_router
from api.routes.analytics import router as analytics_router
from api.routes.auth import router as auth_router
from api.routes.calculator import router as calculator_router
from api.routes.performance import router as performance_router
from api.routes.market import router as market_router
from api.routes.intelligence import router as intelligence_router
from api.routes.plans import router as plans_router
from api.routes.signals import router as signals_router
from api.routes.user_features import router as user_features_router
from api.routes.web_push import router as web_push_router
from api.services.analytics import MarketAnalyticsService
from api.services.auth import AuthService
from api.services.market import FEED_STATUS_KEY, MarketDataService
from api.services.market_intelligence import MarketIntelligenceService
from api.services.plans import PlanService
from api.services.payments import PaymentService, ChainVerifier, BscRpcVerifier
from api.services.referrals import ReferralService
from api.services.support import SupportService
from api.services.admin import AdminService
from api.services.business_notifications import BusinessNotificationService
from api.services.telegram import TelegramBotAPI
from api.services.user_features import AlertPreferencesService, NotificationService, PriceAlertService, WatchlistService
from api.services.web_push import PyWebPushGateway, WebPushService
from worker.binance_rest import BinanceRestClient

configure_logging()
logger = logging.getLogger("ftrade.api")

PRODUCTION_REQUIRED_WORKERS = (
    "market-feed",
    "signal-scanner",
    "signal-lifecycle",
    "plan-scheduler",
    "user-alert-dispatcher",
    "business-reconciler",
    "telegram-notification-delivery",
    "telegram-auth-bot",
    "web-push-delivery",
)


def _is_production() -> bool:
    return os.getenv("FTRADE_ENV", "development").strip().lower() == "production"


def _placeholder(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    return not normalized or any(marker in normalized for marker in (
        "replace-with", "your_new", "replace_me", "placeholder", "change-me", "changeme"
    ))


def _production_configuration_issues() -> list[str]:
    """Return safe-to-expose readiness issue codes without leaking configuration values."""
    if not _is_production():
        return []

    issues: list[str] = []
    access_secret = os.getenv("ACCESS_TOKEN_SECRET") or os.getenv("JWT_SECRET_KEY")
    pepper = os.getenv("AUTH_CODE_PEPPER")
    if _placeholder(access_secret) or len(str(access_secret or "")) < 32:
        issues.append("ACCESS_TOKEN_SECRET_MISSING_OR_WEAK")
    if _placeholder(pepper) or len(str(pepper or "")) < 32:
        issues.append("AUTH_CODE_PEPPER_MISSING_OR_WEAK")
    if not _placeholder(access_secret) and not _placeholder(pepper) and access_secret == pepper:
        issues.append("AUTH_SECRETS_MUST_DIFFER")
    try:
        max_ttl = int(os.getenv("ACCESS_TOKEN_MAX_TTL_SECONDS", "900"))
        if max_ttl < 60 or max_ttl > 3600:
            issues.append("ACCESS_TOKEN_MAX_TTL_OUT_OF_RANGE")
    except (TypeError, ValueError):
        issues.append("ACCESS_TOKEN_MAX_TTL_INVALID")
    admin_ids = {part.strip() for part in os.getenv("ADMIN_USER_IDS", "").split(",") if part.strip()}
    admin_phones = {part.strip() for part in os.getenv("ADMIN_PHONE_NUMBERS", "").split(",") if part.strip()}
    if not admin_ids and not admin_phones:
        issues.append("ADMIN_USER_IDS_EMPTY")

    try:
        if not TelegramBotAPI().is_configured():
            issues.append("TELEGRAM_BOT_NOT_CONFIGURED")
    except Exception:
        issues.append("TELEGRAM_BOT_CONFIGURATION_INVALID")

    try:
        verifier = BscRpcVerifier()
        if not verifier.is_configured():
            issues.append("USDT_BEP20_PAYMENTS_NOT_CONFIGURED")
        rpc_url = os.getenv("BSC_RPC_URL", "https://bsc-dataseed.binance.org").strip()
        if not rpc_url.startswith("https://"):
            issues.append("BSC_RPC_URL_MUST_USE_HTTPS")
        price = Decimal(os.getenv("PREMIUM_PRICE_USDT", "10.00"))
        if not price.is_finite() or price <= 0 or price.quantize(Decimal("0.001")) != price:
            issues.append("PREMIUM_PRICE_INVALID")
        elif price != Decimal("10.00"):
            issues.append("PREMIUM_PRICE_MUST_BE_10_USDT")
    except (InvalidOperation, TypeError, ValueError):
        issues.append("PAYMENT_CONFIGURATION_INVALID")

    try:
        configured_trial_days = int(os.getenv("TRIAL_DAYS", "5"))
        if configured_trial_days != 5:
            if "TRIAL_DAYS_MUST_BE_5" not in issues:
                issues.append("TRIAL_DAYS_MUST_BE_5")
    except (TypeError, ValueError):
        issues.append("TRIAL_DAYS_INVALID")

    origins = [value.strip() for value in os.getenv("CORS_ORIGINS", "").split(",") if value.strip()]
    invalid_origin = False
    for origin in origins:
        parsed = urlparse(origin)
        if parsed.scheme != "https" or not parsed.netloc or "localhost" in parsed.netloc.lower() or "127.0.0.1" in parsed.netloc:
            invalid_origin = True
            break
    if not origins or invalid_origin:
        issues.append("CORS_ORIGINS_MISSING_OR_INSECURE")

    vapid_public = os.getenv("VAPID_PUBLIC_KEY", "").strip()
    vapid_private_path = os.getenv("VAPID_PRIVATE_KEY_PATH", "").strip()
    vapid_private_pem = os.getenv("VAPID_PRIVATE_KEY_PEM", "").strip()
    vapid_private_pem_b64 = os.getenv("VAPID_PRIVATE_KEY_PEM_B64", "").strip()
    vapid_subject = os.getenv("VAPID_SUBJECT", "").strip()
    private_bytes: bytes | None = None
    try:
        if vapid_private_path:
            candidate = Path(vapid_private_path)
            if candidate.is_file():
                private_bytes = candidate.read_bytes()
        if private_bytes is None and vapid_private_pem:
            private_bytes = vapid_private_pem.encode("utf-8")
        if private_bytes is None and vapid_private_pem_b64:
            private_bytes = base64.b64decode(vapid_private_pem_b64, validate=True)
    except Exception:
        private_bytes = None
    if private_bytes is None or not vapid_public:
        issues.append("WEB_PUSH_VAPID_NOT_CONFIGURED")
    else:
        try:
            raw_public = base64.urlsafe_b64decode(vapid_public + "=" * (-len(vapid_public) % 4))
        except Exception:
            raw_public = b""
        if len(raw_public) != 65 or raw_public[:1] != b"\x04":
            issues.append("WEB_PUSH_VAPID_PUBLIC_KEY_INVALID")
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            private_key = serialization.load_pem_private_key(private_bytes, password=None)
            if not isinstance(private_key, ec.EllipticCurvePrivateKey) or private_key.curve.name != "secp256r1":
                issues.append("WEB_PUSH_VAPID_PRIVATE_KEY_INVALID")
            else:
                derived = private_key.public_key().public_bytes(
                    encoding=serialization.Encoding.X962,
                    format=serialization.PublicFormat.UncompressedPoint,
                )
                if raw_public and derived != raw_public:
                    issues.append("WEB_PUSH_VAPID_KEYPAIR_MISMATCH")
        except ImportError:
            issues.append("WEB_PUSH_CRYPTO_DEPENDENCY_MISSING")
        except Exception:
            issues.append("WEB_PUSH_VAPID_PRIVATE_KEY_INVALID")
    if not (vapid_subject.startswith("mailto:") or vapid_subject.startswith("https://")):
        issues.append("WEB_PUSH_VAPID_SUBJECT_INVALID")

    deployment_name = os.getenv("FTRADE_DEPLOYMENT_NAME", "").strip()
    if not deployment_name or deployment_name.lower() in {"development", "default", "test"}:
        issues.append("FTRADE_DEPLOYMENT_NAME_NOT_SET_FOR_PRODUCTION")
    return issues


def _status_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
        return dt.timestamp()
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def create_app(
    repository: SignalRepository | None = None,
    *,
    market_cache: JsonCache | None = None,
    market_rest_client: Any | None = None,
    auth_repository: AuthRepository | None = None,
    telegram_gateway: Any | None = None,
    sweep_store: SweepStore | None = None,
    user_feature_repository: UserFeatureRepository | None = None,
    business_repository: Any | None = None,
    chain_verifier: ChainVerifier | None = None,
    web_push_gateway: Any | None = None,
) -> FastAPI:
    selected_repository = repository or repository_from_environment()
    selected_auth_repository = auth_repository or auth_repository_from_environment()
    selected_cache = market_cache or cache_from_environment()
    # The API serves user-facing reads and must fail fast rather than letting a
    # three-retry exchange timeout occupy request workers. Trading workers create
    # their own client and keep their independent scan/retry policy.
    try:
        market_api_timeout = max(2.0, min(8.0, float(os.getenv("MARKET_API_BINANCE_TIMEOUT_SECONDS", "5"))))
    except (TypeError, ValueError):
        market_api_timeout = 5.0
    try:
        market_api_connections = max(4, min(16, int(os.getenv("MARKET_API_BINANCE_MAX_CONNECTIONS", "8"))))
    except (TypeError, ValueError):
        market_api_connections = 8
    try:
        market_api_concurrency = max(2, min(market_api_connections, int(os.getenv("MARKET_API_BINANCE_CONCURRENCY", "4"))))
    except (TypeError, ValueError):
        market_api_concurrency = 4
    selected_rest = market_rest_client or BinanceRestClient(
        timeout_seconds=market_api_timeout, retries=1,
        max_connections=market_api_connections, concurrency=market_api_concurrency,
    )
    selected_telegram = telegram_gateway or TelegramBotAPI()
    if user_feature_repository is not None:
        selected_user_features = user_feature_repository
    elif isinstance(selected_auth_repository, InMemoryAuthRepository):
        selected_user_features = InMemoryUserFeatureRepository()
    else:
        selected_user_features = user_feature_repository_from_environment()
    if sweep_store is not None:
        selected_sweep_store = sweep_store
    elif isinstance(selected_repository, InMemorySignalRepository):
        # Explicit in-memory app instances are used by local tests, including auth
        # tests that emulate production token checks without external services.
        selected_sweep_store = InMemorySweepStore()
    else:
        selected_sweep_store = sweep_store_from_environment()
    selected_business_repository = business_repository or business_repository_from_environment(selected_auth_repository)
    market_service = MarketDataService(selected_cache, selected_rest)
    analytics_service = MarketAnalyticsService(selected_cache, selected_rest)
    auth_service = AuthService(selected_auth_repository, selected_telegram)
    plan_service = PlanService(selected_auth_repository)
    notification_service = NotificationService(selected_user_features)
    business_notification_service = BusinessNotificationService(selected_auth_repository, selected_user_features, selected_cache)
    referral_service = ReferralService(selected_auth_repository, selected_business_repository, plan_service, business_notifications=business_notification_service)
    payment_service = PaymentService(selected_business_repository, plan_service, chain_verifier, referral_service=referral_service, business_notifications=business_notification_service)
    support_service = SupportService(selected_business_repository)
    admin_service = AdminService(selected_auth_repository, selected_business_repository, plan_service)
    watchlist_service = WatchlistService(selected_auth_repository, market_service, selected_cache, selected_repository, selected_sweep_store)
    alert_preferences_service = AlertPreferencesService(selected_auth_repository)
    price_alert_service = PriceAlertService(selected_auth_repository, selected_user_features, market_service, selected_cache)
    selected_web_push_gateway = web_push_gateway or PyWebPushGateway()
    web_push_service = WebPushService(selected_user_features, selected_web_push_gateway)
    market_intelligence_service = MarketIntelligenceService(selected_cache, selected_rest, selected_repository, selected_sweep_store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.signal_repository = selected_repository
        app.state.auth_repository = selected_auth_repository
        app.state.market_cache = selected_cache
        app.state.market_service = market_service
        app.state.analytics_service = analytics_service
        app.state.auth_service = auth_service
        app.state.plan_service = plan_service
        app.state.business_repository = selected_business_repository
        app.state.payment_service = payment_service
        app.state.referral_service = referral_service
        app.state.support_service = support_service
        app.state.admin_service = admin_service
        app.state.user_feature_repository = selected_user_features
        app.state.watchlist_service = watchlist_service
        app.state.alert_preferences_service = alert_preferences_service
        app.state.price_alert_service = price_alert_service
        app.state.notification_service = notification_service
        app.state.business_notification_service = business_notification_service
        app.state.web_push_service = web_push_service
        app.state.web_push_gateway = selected_web_push_gateway
        app.state.sweep_store = selected_sweep_store
        app.state.market_intelligence_service = market_intelligence_service
        app.state.cancelled_display_minutes = max(5, int(os.getenv("CANCELLED_DISPLAY_MINUTES", "30")))
        yield
        closer = getattr(selected_repository, "close", None)
        if callable(closer):
            closer()
        sweep_closer = getattr(selected_sweep_store, "close", None)
        if callable(sweep_closer):
            sweep_closer()
        features_closer = getattr(selected_user_features, "close", None)
        if callable(features_closer):
            features_closer()
        business_closer = getattr(selected_business_repository, "close", None)
        if callable(business_closer):
            business_closer()
        auth_closer = getattr(selected_auth_repository, "close", None)
        if callable(auth_closer):
            auth_closer()
        try:
            await market_service.close()
        finally:
            try:
                await selected_cache.close()
            finally:
                gateway_closer = getattr(selected_web_push_gateway, "close", None)
                if callable(gateway_closer):
                    gateway_closer()

    app = FastAPI(
        title="KERYNTIX MARKET DESK API",
        version="0.21.0",
        description="API de KERYNTIX MARKET DESK para mercado, señales, cuentas, planes, pagos Premium, referidos, notificaciones transaccionales, soporte, Web Push, administración y readiness.",
        lifespan=lifespan,
    )
    app.state.signal_repository = selected_repository
    app.state.auth_repository = selected_auth_repository
    app.state.market_cache = selected_cache
    app.state.market_service = market_service
    app.state.analytics_service = analytics_service
    app.state.auth_service = auth_service
    app.state.plan_service = plan_service
    app.state.business_repository = selected_business_repository
    app.state.payment_service = payment_service
    app.state.referral_service = referral_service
    app.state.support_service = support_service
    app.state.admin_service = admin_service
    app.state.user_feature_repository = selected_user_features
    app.state.watchlist_service = watchlist_service
    app.state.alert_preferences_service = alert_preferences_service
    app.state.price_alert_service = price_alert_service
    app.state.notification_service = notification_service
    app.state.business_notification_service = business_notification_service
    app.state.web_push_service = web_push_service
    app.state.web_push_gateway = selected_web_push_gateway
    app.state.sweep_store = selected_sweep_store
    app.state.market_intelligence_service = market_intelligence_service
    app.state.cancelled_display_minutes = max(5, int(os.getenv("CANCELLED_DISPLAY_MINUTES", "30")))

    @app.middleware("http")
    async def security_response_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if os.getenv("FTRADE_ENV", "development").strip().lower() == "production":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    # Compress JSON responses (e.g. /market/quotes is ~400 KB of repetitive JSON).
    # Starlette's default level (9) is CPU-heavy; level 4 keeps ~85-90% of the savings.
    # Added before CORS so CORS stays the outermost middleware. WebSockets are not touched.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=int(os.getenv("GZIP_LEVEL", "4")))

    cors_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
        )

    app.include_router(auth_router)
    app.include_router(compatibility_router)
    app.include_router(events_router)
    app.include_router(symbols_router)
    app.include_router(calculator_router)
    app.include_router(performance_router)
    app.include_router(account_router)
    app.include_router(plans_router)
    app.include_router(signals_router)
    app.include_router(market_router)
    app.include_router(analytics_router)
    app.include_router(intelligence_router)
    app.include_router(user_features_router)
    app.include_router(web_push_router)
    app.include_router(billing_router)
    app.include_router(referrals_router)
    app.include_router(support_router)
    app.include_router(admin_router)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            payload = detail
        else:
            default_code = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 410: "GONE", 422: "VALIDATION_ERROR", 429: "TOO_MANY_REQUESTS", 503: "SERVICE_UNAVAILABLE"}.get(exc.status_code, "INTERNAL_ERROR")
            payload = {"code": default_code, "message": str(detail)}
        return JSONResponse(status_code=exc.status_code, content=payload, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"code": "VALIDATION_ERROR", "message": "La solicitud contiene parámetros inválidos.", "details": jsonable_encoder(exc.errors())},
        )

    @app.get("/health", tags=["operations"])
    async def health(request: Request):
        try:
            storage = request.app.state.signal_repository.health()
            auth_storage = request.app.state.auth_repository.health()
            sweep_storage = request.app.state.sweep_store.health()
            user_feature_storage = request.app.state.user_feature_repository.health()
            business_storage = request.app.state.business_repository.health()
            market_cache_health = await request.app.state.market_cache.health()
            try:
                feed_status = await request.app.state.market_service.feed_status()
                market_feed_status = feed_status.model_dump()
            except Exception as exc:
                market_feed_status = {"status": "unavailable", "source": "none", "connected": False,
                                      "stale_seconds": None, "message": type(exc).__name__}
            try:
                raw_feed_status = await request.app.state.market_cache.get_json(FEED_STATUS_KEY)
                if isinstance(raw_feed_status, dict):
                    market_feed_status["connection_count"] = raw_feed_status.get("connection_count")
                    market_feed_status["last_message_at"] = raw_feed_status.get("last_message_at")
                    market_feed_status["last_price_update_at"] = raw_feed_status.get("last_price_update_at")
            except Exception:
                pass
            try:
                signal_engine = await request.app.state.market_cache.get_json("signal-engine:status:v1")
            except Exception:
                signal_engine = {"status": "unavailable"}
            if not isinstance(signal_engine, dict):
                signal_engine = {"status": "not_started"}
            try:
                signal_lifecycle = await request.app.state.market_cache.get_json("signal-lifecycle:status:v1")
            except Exception:
                signal_lifecycle = {"status": "unavailable"}
            if not isinstance(signal_lifecycle, dict):
                signal_lifecycle = {"status": "not_started"}
            try:
                plan_maintenance = await request.app.state.market_cache.get_json("plan-maintenance:status:v1")
            except Exception:
                plan_maintenance = {"status": "unavailable"}
            if not isinstance(plan_maintenance, dict):
                plan_maintenance = {"status": "not_started"}
            try:
                user_alert_dispatcher = await request.app.state.market_cache.get_json("user-alerts:status:v1")
            except Exception:
                user_alert_dispatcher = {"status": "unavailable"}
            if not isinstance(user_alert_dispatcher, dict):
                user_alert_dispatcher = {"status": "not_started"}
            try:
                business_reconciliation = await request.app.state.market_cache.get_json("business-reconciliation:status:v1")
            except Exception:
                business_reconciliation = {"status": "unavailable"}
            if not isinstance(business_reconciliation, dict):
                business_reconciliation = {"status": "not_started"}
            try:
                telegram_notification_delivery = await request.app.state.market_cache.get_json(
                    "telegram-notification-delivery:status:v1"
                )
            except Exception:
                telegram_notification_delivery = {"status": "unavailable"}
            if not isinstance(telegram_notification_delivery, dict):
                telegram_notification_delivery = {"status": "not_started"}
            try:
                web_push_delivery = await request.app.state.market_cache.get_json("web-push-delivery:status:v1")
            except Exception:
                web_push_delivery = {"status": "unavailable"}
            if not isinstance(web_push_delivery, dict):
                web_push_delivery = {"status": "not_started"}
            deployment_namespace = os.getenv("FTRADE_DEPLOYMENT_NAME", os.getenv("FTRADE_ENV", "development")).strip().lower()
            deployment_namespace = "".join(char if char.isalnum() or char in "-_" else "-" for char in deployment_namespace).strip("-") or "default"
            now_epoch = time.time()
            try:
                worker_lease_ttl = max(15, min(3600, int(os.getenv("WORKER_LEASE_TTL_SECONDS", "90"))))
            except (TypeError, ValueError):
                worker_lease_ttl = 90
            worker_stale_after = max(60, worker_lease_ttl * 2)
            worker_statuses: dict[str, Any] = {}
            worker_names = PRODUCTION_REQUIRED_WORKERS
            for worker_name in worker_names:
                try:
                    worker_status = await request.app.state.market_cache.get_json(
                        f"ftrade:worker-status:{deployment_namespace}:{worker_name}:v1"
                    )
                except Exception:
                    worker_status = None
                if not isinstance(worker_status, dict):
                    worker_statuses[worker_name] = {"status": "not_started"}
                    continue
                status_copy = dict(worker_status)
                updated_epoch = _status_timestamp(status_copy.get("updated_at"))
                if status_copy.get("status") == "running" and (
                    updated_epoch is None or now_epoch - updated_epoch > worker_stale_after
                ):
                    status_copy["last_status"] = status_copy.get("status")
                    status_copy["status"] = "stale"
                worker_statuses[worker_name] = status_copy

            background_statuses = (signal_engine, signal_lifecycle, plan_maintenance, user_alert_dispatcher,
                                   business_reconciliation, telegram_notification_delivery, web_push_delivery)
            background_degraded = any(
                isinstance(item, dict) and item.get("status") in {"degraded", "failed", "unavailable", "not_configured"}
                for item in background_statuses
            )
            production = _is_production()
            configuration_issues = _production_configuration_issues()
            worker_issues: list[str] = []
            if production:
                for worker_name, status_doc in worker_statuses.items():
                    if status_doc.get("status") != "running":
                        worker_issues.append(f"WORKER_NOT_HEALTHY:{worker_name}:{status_doc.get('status', 'unknown')}")
                if market_cache_health.get("shared") is not True:
                    worker_issues.append("SHARED_REDIS_REQUIRED")
            storage_issues: list[str] = []
            if storage.get("status") != "ok": storage_issues.append("SIGNAL_STORAGE_UNAVAILABLE")
            if auth_storage.get("status") != "ok": storage_issues.append("AUTH_STORAGE_UNAVAILABLE")
            if sweep_storage.get("status") != "ok": storage_issues.append("SWEEP_STORAGE_UNAVAILABLE")
            if user_feature_storage.get("status") != "ok": storage_issues.append("USER_FEATURE_STORAGE_UNAVAILABLE")
            if business_storage.get("status") != "ok": storage_issues.append("BUSINESS_STORAGE_UNAVAILABLE")
            if market_cache_health.get("status") != "ok": storage_issues.append("MARKET_CACHE_UNAVAILABLE")
            if background_degraded: storage_issues.append("BACKGROUND_TASK_DEGRADED")

            readiness_issues = sorted(set(configuration_issues + worker_issues + storage_issues))
            degraded = bool(readiness_issues)
            return {
                "status": "degraded" if degraded else "ok",
                "service": "ftrade-api",
                "configuration": {"status": "degraded" if configuration_issues else ("ok" if production else "not_required"),
                                  "issues": configuration_issues},
                "readiness_issues": readiness_issues,
                "storage": storage,
                "auth_storage": auth_storage,
                "sweep_storage": sweep_storage,
                "user_feature_storage": user_feature_storage,
                "business_storage": business_storage,
                "market_cache": market_cache_health,
                "market_feed": market_feed_status,
                "signal_engine": signal_engine,
                "signal_lifecycle": signal_lifecycle,
                "plan_maintenance": plan_maintenance,
                "user_alert_dispatcher": user_alert_dispatcher,
                "business_reconciliation": business_reconciliation,
                "telegram_notification_delivery": telegram_notification_delivery,
                "web_push_delivery": web_push_delivery,
                "workers": worker_statuses,
            }
        except Exception:
            logger.exception("Health check no disponible")
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "service": "ftrade-api", "storage": {"status": "error"}, "auth_storage": {"status": "error"}, "business_storage": {"status": "error"}, "sweep_storage": {"status": "error"}, "user_feature_storage": {"status": "error"}, "market_cache": {"status": "error"}, "market_feed": {"status": "unavailable", "source": "none", "connected": False}, "signal_engine": {"status": "unavailable"}, "signal_lifecycle": {"status": "unavailable"}, "plan_maintenance": {"status": "unavailable"}, "user_alert_dispatcher": {"status": "unavailable"}, "business_reconciliation": {"status": "unavailable"}, "telegram_notification_delivery": {"status": "unavailable"}, "web_push_delivery": {"status": "unavailable"}, "workers": {}},
            )

    @app.get("/ready", tags=["operations"])
    async def readiness(request: Request):
        """Deployment readiness: 503 while required config, storage or workers are unhealthy."""
        snapshot = await health(request)
        if isinstance(snapshot, JSONResponse):
            return snapshot
        payload = dict(snapshot)
        payload["ready"] = payload.get("status") == "ok"
        if not payload["ready"]:
            return JSONResponse(status_code=503, content=jsonable_encoder(payload))
        return payload

    @app.get("/live", tags=["operations"])
    def liveness():
        """Process liveness only; do not fail restarts because a dependency is temporarily degraded."""
        return {"status": "ok", "service": "ftrade-api"}

    @app.get("/", tags=["operations"])
    def root():
        return {"service": "ftrade-api", "version": "0.20.0", "phase": "web-push-delivery"}

    return app


app = create_app()
