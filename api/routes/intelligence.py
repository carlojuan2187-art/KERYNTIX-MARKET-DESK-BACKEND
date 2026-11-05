"""REST endpoints for radar, liquidation tape, movers and dashboard overview."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from api.repositories.signals import RepositoryUnavailable
from api.security import Principal, require_analytical_access
from domain.plans import evaluate_access
from api.services.market_intelligence import AnalyticsIntelligenceUnavailable

router = APIRouter(tags=["market-intelligence"])


def _clean_setup(row: dict[str, Any]) -> dict[str, Any] | None:
    lifecycle = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    status = str(row.get("status") or lifecycle.get("status") or "").upper()
    if status not in {"PENDING", "TOUCHED"}:
        return None
    created = row.get("created_at")
    if hasattr(created, "isoformat"):
        created = created.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if created.tzinfo else created.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    entry_zone = row.get("entry_zone") if isinstance(row.get("entry_zone"), dict) else None
    return {
        "id": str(row.get("id") or row.get("_id") or ""),
        "symbol": row.get("symbol"), "direction": row.get("direction"), "status": status,
        "visibility": row.get("visibility", "premium"), "entry_price": row.get("entry_price"),
        "entry_zone": entry_zone, "stop_loss": row.get("stop_loss"),
        "take_profits": row.get("take_profits") or [], "score": row.get("score"),
        "components": row.get("components") or [], "created_at": created,
        "touched_at": row.get("touched_at"), "entry_expires_at": row.get("entry_expires_at"),
        "liquidity_sweep_extreme_price": row.get("liquidity_sweep_extreme_price"),
        "liquidity_zone": row.get("liquidity_zone"), "atr_value": row.get("atr_value"),
    }


@router.get("/radar")
async def get_radar(
    request: Request,
    response: Response,
    refresh: bool = Query(default=False),
    _principal: Principal = Depends(require_analytical_access),
) -> dict[str, Any]:
    try:
        result = await request.app.state.market_intelligence_service.radar(force_refresh=refresh)
        records, setups_refreshing, signal_updated_at = await request.app.state.market_intelligence_service.signal_records(wait_seconds=0.0)
    except AnalyticsIntelligenceUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "RADAR_UNAVAILABLE", "message": "El radar está recopilando cotizaciones y velas del mercado."}) from exc
    except RepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "SIGNALS_UNAVAILABLE", "message": "No se pudieron leer los setups activos."}) from exc
    # A live five-day trial has the same feature entitlement as a paid Premium
    # subscription. Do not infer Premium access from the stored plan label alone:
    # trial accounts intentionally keep plan="free" until they purchase.
    entitlement = evaluate_access(
        _principal.claims,
        now=datetime.now(timezone.utc),
        is_admin=_principal.is_admin,
    )
    can_see_premium = bool(entitlement.get("allowed"))
    setups = []
    seen = set()
    now = datetime.now(timezone.utc)
    for raw in records:
        row = dict(raw)
        visibility = str(row.get("visibility") or "premium").lower()
        if visibility not in {"free", "public"} and not can_see_premium:
            continue
        setup = _clean_setup(row)
        if not setup:
            continue
        expires = row.get("entry_expires_at") or row.get("valid_until")
        if isinstance(expires, str):
            try:
                expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except ValueError:
                expires = None
        if isinstance(expires, datetime):
            expires = expires.replace(tzinfo=timezone.utc) if expires.tzinfo is None else expires.astimezone(timezone.utc)
            if setup["status"] == "PENDING" and expires <= now:
                continue
        key = (setup.get("symbol"), setup.get("direction"))
        if key in seen:
            continue
        seen.add(key)
        setups.append(setup)
        if len(setups) >= 15:
            break
    output = dict(result)
    output["setups"] = setups
    output["setups_refreshing"] = bool(setups_refreshing)
    output["setups_updated_at"] = signal_updated_at

    # The radar snapshot scans the most liquid pairs, but active setups and old
    # observed sweeps can reference other symbols. Enrich every displayed symbol
    # with a cached, real 15M/1H/4H candle analysis rather than leaving the UI null.
    trend_symbols: list[str] = []
    def include_symbol(value: Any) -> None:
        symbol = str(value or "").strip().upper()
        if symbol.endswith("USDT") and "_" not in symbol and symbol not in trend_symbols:
            trend_symbols.append(symbol)

    for setup in setups:
        include_symbol(setup.get("symbol"))
    for key in ("items", "sweeps", "watch", "movers"):
        entries = result.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                include_symbol(entry.get("symbol"))
    existing_trends = result.get("timeframe_trends")
    existing_trends = existing_trends if isinstance(existing_trends, dict) else {}
    if isinstance(existing_trends, dict):
        for symbol in existing_trends:
            include_symbol(symbol)

    # The radar scan fetches multiple candle intervals for up to 18 assets.
    # While its shared snapshot is being refreshed, serve the last known trends
    # (or an empty map on cold start) instead of launching another batch of REST
    # candle requests in the same HTTP request. On a fresh snapshot, supplement
    # only a few active setups missing from the cached scan to bound request time.
    trends = dict(existing_trends)
    if not result.get("refreshing"):
        missing = [symbol for symbol in trend_symbols if symbol not in trends][:4]
        if missing:
            try:
                trends.update(await request.app.state.market_intelligence_service.timeframe_trends_for_symbols(missing))
            except Exception:
                # Trend enrichment is optional; it must not make the entire radar unavailable.
                pass
    output["timeframe_trends"] = trends

    response.headers["X-FTrade-Radar-Source"] = str(result.get("source", ""))
    response.headers["X-FTrade-Radar-Updated-At"] = str(result.get("updated_at", ""))
    return output


@router.get("/market/liquidations")
@router.get("/liquidations")
async def get_liquidations(
    request: Request,
    response: Response,
    window: int = Query(default=15),
    _principal: Principal = Depends(require_analytical_access),
) -> dict[str, Any]:
    if window not in (5, 15, 60):
        raise HTTPException(status_code=422, detail={"code": "INVALID_LIQUIDATIONS_WINDOW", "message": "window debe ser 5, 15 o 60 minutos."})
    data = await request.app.state.market_intelligence_service.liquidations(window)
    response.headers["X-FTrade-Liquidations-Source"] = str(data.get("source", ""))
    response.headers["X-FTrade-Liquidations-Status"] = str(data.get("status", "unavailable"))
    return data


@router.get("/market/movers")
@router.get("/movers")
async def get_movers(
    request: Request,
    response: Response,
    window: int = Query(default=15),
    refresh: bool = Query(default=False),
    _principal: Principal = Depends(require_analytical_access),
) -> dict[str, Any]:
    if window not in (5, 15, 60):
        raise HTTPException(status_code=422, detail={"code": "INVALID_MOVERS_WINDOW", "message": "window debe ser 5, 15 o 60 minutos."})
    data = await request.app.state.market_intelligence_service.movers(window, force_refresh=refresh)
    response.headers["X-FTrade-Movers-Source"] = str(data.get("source", ""))
    response.headers["X-FTrade-Movers-Status"] = str(data.get("status", "unavailable"))
    return data


@router.get("/overview")
async def get_overview(
    request: Request,
    response: Response,
    _principal: Principal = Depends(require_analytical_access),
) -> dict[str, Any]:
    try:
        data = await request.app.state.market_intelligence_service.overview()
    except RepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "OVERVIEW_UNAVAILABLE", "message": "El resumen no está disponible temporalmente."}) from exc
    response.headers["X-FTrade-Overview-Source"] = str(data.get("source", ""))
    response.headers["X-FTrade-Overview-Status"] = str(data.get("status", "degraded"))
    return data
