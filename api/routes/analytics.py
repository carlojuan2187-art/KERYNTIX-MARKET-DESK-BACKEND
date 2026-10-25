"""REST routes for the original FTrade market-analysis modules."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from api.security import Principal, require_analytical_access
from api.services.analytics import AnalyticsUnavailable
from worker.binance_rest import BinanceDataError, BinanceInvalidSymbolError

router = APIRouter(tags=["market-analytics"])


@router.get("/market/regime")
async def get_market_regime(request: Request, response: Response,
                            _principal: Principal = Depends(require_analytical_access)) -> dict:
    try:
        data = await request.app.state.analytics_service.regime()
    except (AnalyticsUnavailable, BinanceDataError) as exc:
        raise HTTPException(status_code=503, detail={
            "code": "REGIME_UNAVAILABLE",
            "message": "El análisis de régimen está recopilando datos o no está disponible temporalmente.",
        }) from exc
    response.headers["X-FTrade-Analytics-Source"] = "binance-futures"
    response.headers["X-FTrade-Analytics-Updated-At"] = str(data.get("updated_at", ""))
    return data


@router.get("/derivatives")
async def get_derivatives(request: Request, response: Response,
                          symbol: str = Query(default="BTCUSDT", min_length=6, max_length=24),
                          _principal: Principal = Depends(require_analytical_access)) -> dict:
    try:
        data = await request.app.state.analytics_service.derivatives(symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "INVALID_SYMBOL", "message": "El símbolo debe ser un par válido de Binance USDT-M Futures.",
        }) from exc
    except AnalyticsUnavailable as exc:
        raise HTTPException(status_code=503, detail={
            "code": "DERIVATIVES_UNAVAILABLE", "message": "Binance no está proporcionando métricas de derivados para este par.",
        }) from exc
    response.headers["X-FTrade-Analytics-Source"] = "binance-futures"
    response.headers["X-FTrade-Analytics-Updated-At"] = str(data.get("updated_at", ""))
    return data


@router.get("/market/flow")
async def get_market_flow(request: Request, response: Response,
                          symbol: str = Query(default="BTCUSDT", min_length=6, max_length=24),
                          _principal: Principal = Depends(require_analytical_access)) -> dict:
    try:
        data = await request.app.state.analytics_service.flow(symbol, str(_principal.claims.get("tz") or "UTC"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "INVALID_SYMBOL", "message": "El símbolo debe ser un par válido de Binance USDT-M Futures.",
        }) from exc
    except BinanceInvalidSymbolError as exc:
        raise HTTPException(status_code=404, detail={
            "code": "SYMBOL_NOT_FOUND", "message": "Binance no reconoce el par solicitado.",
        }) from exc
    except (AnalyticsUnavailable, BinanceDataError) as exc:
        raise HTTPException(status_code=503, detail={
            "code": "FLOW_COLLECTING", "message": "El flujo necesita suficientes velas recientes de 5 minutos.",
        }) from exc
    response.headers["X-FTrade-Analytics-Source"] = "binance-futures-klines-5m"
    response.headers["X-FTrade-Analytics-Updated-At"] = str(data.get("updated_at", ""))
    return data


@router.get("/calendar")
async def get_calendar(request: Request, response: Response,
                       view: int = Query(default=7, ge=7, le=30),
                       _principal: Principal = Depends(require_analytical_access)) -> list[dict]:
    if view not in (7, 30):
        raise HTTPException(status_code=422, detail={
            "code": "INVALID_CALENDAR_WINDOW", "message": "view debe ser 7 o 30 días.",
        })
    try:
        result = await request.app.state.analytics_service.calendar(view)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={
            "code": "CALENDAR_UNAVAILABLE", "message": "El calendario no está disponible temporalmente.",
        }) from exc
    response.headers["X-FTrade-Calendar-BLS"] = "available" if result.get("bls_ok") else "degraded"
    response.headers["X-FTrade-Analytics-Updated-At"] = str(result.get("updated_at", ""))
    return result.get("events", [])
