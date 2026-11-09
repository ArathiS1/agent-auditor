from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..canary import service
from .canary_schemas import CanaryEvaluateIn, CanaryReportOut, CanarySummaryOut
from .deps import get_db, require_api_key

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


def _wrap(call):
    try:
        return call()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/agents/{name}/canary/evaluate", response_model=CanaryReportOut)
def evaluate(name: str, body: CanaryEvaluateIn, db: Session = Depends(get_db)) -> dict:
    payload = body.model_dump()
    data = _wrap(lambda: service.canary_report(db, agent=name, **payload))
    db.commit()
    return data


@router.get("/agents/{name}/canary", response_model=list[CanarySummaryOut])
def list_reports(
    name: str, candidate: str | None = None, limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[dict]:
    return _wrap(lambda: service.list_reports(db, name, limit, candidate))


@router.get("/canary/{report_id}", response_model=CanaryReportOut)
def get_report(report_id: str, db: Session = Depends(get_db)) -> dict:
    return _wrap(lambda: service.get_report(db, report_id))
