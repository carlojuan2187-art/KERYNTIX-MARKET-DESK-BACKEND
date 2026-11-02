"""Read-only HTTP endpoints for KERYNTIX MARKET DESK signals."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from api.repositories.signals import RepositoryUnavailable
from api.schemas import SignalApiResponse
from domain.signals import CanonicalSignalResult, OutcomeState
from api.security import Principal, require_analytical_access
from api.services.signals import SignalDataIntegrityError, SignalService
from api.services.stats_baseline import get_reset_at, rows_since

router = APIRouter(prefix="/signals", tags=["signals"])


def _service(request: Request) -> SignalService:
    return SignalService(
        request.app.state.signal_repository,
        cancelled_display_minutes=request.app.state.cancelled_display_minutes,
    )


@router.get("", response_model=list[SignalApiResponse])
def list_signals(
    request: Request,
    limit: int = Query(default=10, ge=1, le=10),
    _principal: Principal = Depends(require_analytical_access),
) -> list[SignalApiResponse]:
    try:
        return _service(request).list_visible(limit=limit)
    except RepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "DATA_UNAVAILABLE", "message": "Los datos de señales no están disponibles temporalmente."}) from exc
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
        rows = request.app.state.signal_repository.list_results(limit=limit)
        # Admin statistics reset: count only trades closed after the cut-off.
        rows = rows_since(rows, get_reset_at(request.app.state.business_repository))
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
    return signal
