"""Authenticated risk calculator endpoint using the bot's original risk model."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from api.schemas_calculator import CalculatorInput, CalculatorOutput
from api.security import Principal, require_analytical_access
from api.services.calculator import calculate_for_web
from engine.risk_calc import RiskInputError

router = APIRouter(tags=["calculator"])


@router.post("/calculator", response_model=CalculatorOutput)
def calculate_position(payload: CalculatorInput, _principal: Principal = Depends(require_analytical_access)) -> CalculatorOutput:
    try:
        result = calculate_for_web(payload.model_dump(exclude_none=True))
    except RiskInputError as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_RISK_INPUT", "message": str(exc)}) from exc
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(status_code=422, detail={"code": "INVALID_RISK_INPUT", "message": "Los niveles de la operación no son válidos."}) from exc
    return CalculatorOutput.model_validate(result)
