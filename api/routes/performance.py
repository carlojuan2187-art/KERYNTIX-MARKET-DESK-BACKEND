"""Historical closed-trade performance endpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.repositories.signals import RepositoryUnavailable
from api.security import Principal, require_analytical_access
from api.services.stats_baseline import get_reset_at
from api.services.user_performance import personal_result_rows
from api.repositories.signals import _parse_datetime
from engine.performance import build_performance

router = APIRouter(prefix="/results", tags=["performance"])


@router.get("")
def get_performance(
    request: Request,
    period: str = Query(default="month", pattern="^(day|week|month|all)$"),
    _principal: Principal = Depends(require_analytical_access),
) -> dict:
    try:
        reset_at = get_reset_at(request.app.state.business_repository)
        repository = request.app.state.signal_repository
        rows = personal_result_rows(
            repository,
            user_id=_principal.user_id,
            claims=_principal.claims,
            reset_at=reset_at,
            is_admin=_principal.is_admin,
        )
        # Preserve the existing Results-page windows: rolling 24h, 7d and 30d.
        # The scoring/aggregation engine remains untouched.
        now = datetime.now(timezone.utc)
        spans = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30)}
        cutoff = now - spans[period] if period in spans else None
        if cutoff is not None:
            rows = [
                row for row in rows
                if (_parse_datetime(row.get("closed_at")) is not None
                    and _parse_datetime(row.get("closed_at")) >= cutoff)
            ]
        result = build_performance(rows, period="all", now=now)
        result["period"] = period
        result["period_start"] = cutoff.isoformat().replace("+00:00", "Z") if cutoff else None
        return result
    except RepositoryUnavailable as exc:
        raise HTTPException(status_code=503, detail={"code": "DATA_UNAVAILABLE", "message": "El histórico de resultados no está disponible temporalmente."}) from exc
    except Exception as exc:
        # Validation/integrity problems must be observable and must not leak internals.
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=503, detail={"code": "RESULT_DATA_UNAVAILABLE", "message": "No se pudieron calcular las estadísticas históricas."}) from exc
