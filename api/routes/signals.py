"""Read-only HTTP endpoints for KERYNTIX MARKET DESK signals."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from api.repositories.signals import RepositoryUnavailable
from api.schemas import SignalApiResponse
from domain.signals import CanonicalSignalResult, OutcomeState
from api.security import Principal, require_analytical_access
from api.services.signals import SignalDataIntegrityError, SignalService
from api.services.stats_baseline import get_reset_at
from api.services.user_performance import personal_result_rows

router = APIRouter(prefix="/signals", tags=["signals"])


def _service(request: Request) -> SignalService:
    return SignalService(
        request.app.state.signal_repository,
        cancelled_display_minutes=request.app.state.cancelled_display_minutes,
    )


@router.get("", response_model=list[SignalApiResponse])
async def list_signals(
    request: Request,
    response: Response,
    limit: int = Query(default=10, ge=1, le=10),
    _principal: Principal = Depends(require_analytical_access),
) -> list[SignalApiResponse]:
    try:
        # The shared Redis snapshot is refreshed off-request. A slow Mongo read
        # must not make every visitor wait 20 seconds for the same 10 live cards.
        records, refreshing, updated_at = await request.app.state.market_intelligence_service.signal_records(wait_seconds=1.5)
        rows = _service(request).list_visible(limit=limit, raw_docs=records)
        # Persist the delivery before returning the feed. A unique user/signal key
        # prevents repeated polling from inflating personal operation counts.
        request.app.state.signal_repository.record_user_deliveries(
            _principal.user_id,
            [str(row.id) for row in rows],
            delivered_at=datetime.now(timezone.utc),
        )
        response.headers["X-Keryntix-Signals-Status"] = "refreshing" if refreshing else "ready"
        if updated_at:
            response.headers["X-Keryntix-Signals-Updated-At"] = updated_at
        response.headers["Cache-Control"] = "private, max-age=2, stale-while-revalidate=30"
        return rows
    except RepositoryUnavailable as exc:
        raise HTTPException(status_code=503, headers={"Retry-After": "2"}, detail={"code": "DATA_UNAVAILABLE", "message": "El feed de señales se está sincronizando. Se reintentará automáticamente."}) from exc
    except SignalDataIntegrityError as exc:
        raise HTTPException(status_code=503, detail={"code": "SIGNAL_DATA_INVALID", "message": "Hay una señal almacenada que no cumple el contrato de datos."}) from exc


@router.get("/results", response_model=list[CanonicalSignalResult])
def list_signal_results(
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    _principal: Principal = Depends(require_analytical_access),
) -> list[CanonicalSignalResult]:
    """Return immutable closed-trade snapshots (TP1/TP2/SL), newest first."""
    try:
        rows = personal_result_rows(
            request.app.state.signal_repository,
            user_id=_principal.user_id,
            claims=_principal.claims,
            reset_at=get_reset_at(request.app.state.business_repository),
            is_admin=_principal.is_admin,
        )
        rows = list(reversed(rows[-limit:]))
    except RepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "DATA_UNAVAILABLE", "message": "Los resultados no están disponibles temporalmente."}) from exc
    output: list[CanonicalSignalResult] = []
    for row in rows:
        data = dict(row)
        data.pop("_id", None)
        data.setdefault("id", str(data.get("signal_id") or ""))
        data.setdefault("schema_version", "1.0")
        try:
            output.append(CanonicalSignalResult.model_validate(data))
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"code": "RESULT_DATA_INVALID", "message": "Hay un resultado almacenado que no cumple el contrato de datos."}) from exc
    return output


@router.get("/{symbol}", response_model=SignalApiResponse)
def get_signal_by_symbol(
    symbol: str,
    request: Request,
    _principal: Principal = Depends(require_analytical_access),
) -> SignalApiResponse:
    normalized = symbol.strip().upper()
    if not normalized or len(normalized) > 40:
        raise HTTPException(status_code=422, detail={"code": "INVALID_SYMBOL", "message": "Símbolo inválido."})
    try:
        signal = _service(request).latest_for_symbol(normalized)
    except RepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "DATA_UNAVAILABLE", "message": "Los datos de señales no están disponibles temporalmente."}) from exc
    except SignalDataIntegrityError as exc:
        raise HTTPException(status_code=503, detail={"code": "SIGNAL_DATA_INVALID", "message": "La señal almacenada no cumple el contrato de datos."}) from exc
    if signal is None:
        raise HTTPException(status_code=404, detail={"code": "SIGNAL_NOT_FOUND", "message": f"No se encontró una señal para {normalized}."})
    request.app.state.signal_repository.record_user_deliveries(
        _principal.user_id,
        [str(signal.id)],
        delivered_at=datetime.now(timezone.utc),
    )
    return signal
