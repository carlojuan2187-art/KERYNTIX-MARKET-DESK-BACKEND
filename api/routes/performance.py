"""Historical closed-trade performance endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.repositories.signals import RepositoryUnavailable
from api.security import Principal, require_analytical_access
from engine.performance import build_performance, period_start

router = APIRouter(prefix="/results", tags=["performance"])


@router.get("")
def get_performance(
    request: Request,
    period: str = Query(default="month", pattern="^(day|week|month|all)$"),
    _principal: Principal = Depends(require_analytical_access),
) -> dict:
    try:
        start = period_start(period)
        repository = request.app.state.signal_repository
        rows = repository.list_results_for_period(start=start)
        return build_performance(rows, period=period)
    except RepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "DATA_UNAVAILABLE", "message": "El histórico de resultados no está disponible temporalmente."}) from exc
    except Exception as exc:
        # Validation/integrity problems must be observable and must not leak internals.
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=503, detail={"code": "RESULT_DATA_UNAVAILABLE", "message": "No se pudieron calcular las estadísticas históricas."}) from exc
