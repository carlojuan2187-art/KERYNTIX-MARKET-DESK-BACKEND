"""Historical closed-trade performance endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.repositories.signals import RepositoryUnavailable
from api.security import Principal, require_analytical_access
from api.services.stats_baseline import get_reset_at, latest_start, rows_since
from engine.performance import build_performance, period_start

router = APIRouter(prefix="/results", tags=["performance"])


@router.get("")
def get_performance(
    request: Request,
    period: str = Query(default="month", pattern="^(day|week|month|all)$"),
    _principal: Principal = Depends(require_analytical_access),
) -> dict:
    try:
        reset_at = get_reset_at(request.app.state.business_repository)
        start = latest_start(period_start(period), reset_at)
        repository = request.app.state.signal_repository
        rows = rows_since(repository.list_results_for_period(start=start), reset_at)
        return build_performance(rows, period=period)
    except RepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "DATA_UNAVAILABLE", "message": "El histórico de resultados no está disponible temporalmente."}) from exc
    except Exception as exc:
        # Validation/integrity problems must be observable and must not leak internals.
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=503, detail={"code": "RESULT_DATA_UNAVAILABLE", "message": "No se pudieron calcular las estadísticas históricas."}) from exc
