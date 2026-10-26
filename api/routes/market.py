"""Market-data endpoints. Response bodies keep the exact frontend contract."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from api.market_schemas import AssetQuote, Candle
from api.security import Principal, require_analytical_access
from worker.binance_rest import BinanceDataError, BinanceInvalidSymbolError

router = APIRouter(prefix="/market", tags=["market"])


def _set_market_headers(response: Response, health) -> None:
    response.headers["X-FTrade-Market-Status"] = health.status
    response.headers["X-FTrade-Market-Source"] = health.source
    response.headers["X-FTrade-Market-Connected"] = str(bool(health.connected)).lower()
    response.headers["X-FTrade-Market-Stale-Seconds"] = "" if health.stale_seconds is None else str(health.stale_seconds)
    if health.updated_at:
        response.headers["X-FTrade-Market-Updated-At"] = health.updated_at


@router.get("/quotes", response_model=list[AssetQuote])
async def get_quotes(
    request: Request,
    response: Response,
    _principal: Principal = Depends(require_analytical_access),
) -> list[AssetQuote]:
    """Return all available USDⓈ-M USDT quotes; cache first, REST fallback if stale."""
    try:
        quotes, health = await request.app.state.market_service.quotes()
    except BinanceDataError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "DATA_UNAVAILABLE",
            "message": "Las cotizaciones de mercado no están disponibles temporalmente.",
        }) from exc
    _set_market_headers(response, health)
    if health.status == "degraded" and health.message:
        response.headers["Warning"] = '110 - "Market data is stale or using fallback"'
    return quotes


@router.get("/candles", response_model=list[Candle])
async def get_candles(
    request: Request,
    response: Response,
    symbol: str = Query(..., min_length=3, max_length=40),
    timeframe: str = Query(default="1h", min_length=2, max_length=3),
    limit: int = Query(default=120, ge=1, le=500),
    tz: str | None = Query(default=None, min_length=1, max_length=64),
    _principal: Principal = Depends(require_analytical_access),
) -> list[Candle]:
    """Return chronological candles. `timestamp` is epoch milliseconds; `time` is local label."""
    chosen_tz = tz or str(_principal.claims.get("tz") or "America/Havana")
    try:
        candles, health = await request.app.state.market_service.candles(
            symbol=symbol, timeframe=timeframe, limit=limit, tz_name=chosen_tz,
        )
    except ValueError as exc:
        reason = str(exc)
        if reason == "INVALID_SYMBOL":
            raise HTTPException(status_code=404, detail={"code": "SYMBOL_NOT_FOUND", "message": "El par solicitado no es válido."}) from exc
        if reason == "INVALID_TIMEFRAME":
            raise HTTPException(status_code=422, detail={"code": "INVALID_TIMEFRAME", "message": "timeframe debe ser 1m, 5m, 15m, 1h, 4h o 1D."}) from exc
        raise HTTPException(status_code=422, detail={"code": "INVALID_TIMEZONE", "message": "La zona horaria no es una zona IANA válida."}) from exc
    except BinanceInvalidSymbolError as exc:
        raise HTTPException(status_code=404, detail={"code": "SYMBOL_NOT_FOUND", "message": "Binance no reconoce el par solicitado."}) from exc
    except BinanceDataError as exc:
        raise HTTPException(status_code=503, detail={"code": "DATA_UNAVAILABLE", "message": "Las velas no están disponibles temporalmente."}) from exc
    _set_market_headers(response, health)
    if health.status == "degraded" and health.message:
        response.headers["Warning"] = '110 - "Market data is stale or using fallback"'
    return candles
