"""Symbol catalogue/search endpoints backed only by real USDⓈ-M quotes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from api.market_schemas import AssetQuote
from api.security import Principal, require_analytical_access
from worker.binance_rest import BinanceDataError

router = APIRouter(prefix="/symbols", tags=["symbols"])


def _normalise_symbol(value: str) -> str:
    clean = "".join(str(value or "").upper().split()).replace("/", "").replace("-", "")
    if clean and not clean.endswith("USDT"):
        clean += "USDT"
    return clean


def _quote_search_blob(quote: AssetQuote) -> str:
    return f"{quote.symbol} {quote.name} {quote.symbol[:-4]}".casefold()


@router.get("", response_model=list[AssetQuote])
async def search_symbols(
    request: Request,
    response: Response,
    query: str | None = Query(default=None, min_length=1, max_length=40),
    limit: int = Query(default=25, ge=1, le=100),
    _principal: Principal = Depends(require_analytical_access),
) -> list[AssetQuote]:
    """Search the current Binance USDT-M catalogue; never returns mock symbols."""
    try:
        quotes, health = await request.app.state.market_service.quotes()
    except BinanceDataError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "SYMBOL_CATALOG_UNAVAILABLE",
            "message": "El catálogo real de símbolos no está disponible temporalmente.",
        }) from exc
    needle = (query or "").strip().casefold()
    if needle:
        quotes = [item for item in quotes if needle in _quote_search_blob(item)]
    quotes.sort(key=lambda item: (item.volume_24h or 0.0), reverse=True)
    response.headers["X-FTrade-Symbol-Catalog-Source"] = health.source
    response.headers["X-FTrade-Symbol-Catalog-Status"] = health.status
    return quotes[:limit]


@router.get("/{symbol}", response_model=AssetQuote)
async def get_symbol(
    symbol: str,
    request: Request,
    response: Response,
    _principal: Principal = Depends(require_analytical_access),
) -> AssetQuote:
    """Resolve a single symbol, accepting BTC or BTCUSDT while preserving 1000 prefixes."""
    normalized = _normalise_symbol(symbol)
    if len(normalized) < 7 or len(normalized) > 34 or not normalized.endswith("USDT") or not normalized[:-4].isalnum():
        raise HTTPException(status_code=422, detail={"code": "INVALID_SYMBOL", "message": "El símbolo solicitado no tiene un formato válido."})
    try:
        quotes, health = await request.app.state.market_service.quotes()
    except BinanceDataError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "SYMBOL_CATALOG_UNAVAILABLE",
            "message": "El catálogo real de símbolos no está disponible temporalmente.",
        }) from exc
    match = next((item for item in quotes if item.symbol == normalized), None)
    if match is None:
        raise HTTPException(status_code=404, detail={"code": "SYMBOL_NOT_FOUND", "message": "Binance no reconoce el símbolo USDⓈ-M solicitado."})
    response.headers["X-FTrade-Symbol-Catalog-Source"] = health.source
    response.headers["X-FTrade-Symbol-Catalog-Status"] = health.status
    return match
