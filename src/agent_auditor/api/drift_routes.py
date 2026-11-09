from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..drift import service
from ..models import DriftReport
from .deps import get_db, require_api_key
from .drift_schemas import DriftCheckIn, DriftReportOut, DriftReportSummary

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


def _full(row: DriftReport) -> DriftReportOut:
    return DriftReportOut(id=row.id, created_at=row.created_at, **row.result)


@router.post("/drift/check", response_model=DriftReportOut)
def check_drift(body: DriftCheckIn, db: Session = Depends(get_db)) -> DriftReportOut:
    try:
        data, row = service.run_drift_check(db, **body.model_dump())
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    if row is None:
        return DriftReportOut(**data)
    return _full(row)


@router.get("/agents/{name}/drift", response_model=list[DriftReportSummary])
def list_drift_reports(name: str, limit: int = Query(default=10, ge=1, le=100), db: Session = Depends(get_db)) -> list[DriftReportSummary]:
    try:
        rows = service.list_reports(db, name, limit)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return [
        DriftReportSummary(
            id=r.id, created_at=r.created_at, as_of=r.as_of, environment=r.environment, version=r.version_label,
            overall=r.overall, reference_runs=r.result.get("reference_runs", 0), current_runs=r.result.get("current_runs", 0),
            flagged=[f"{s['name']}:{s['severity']}" for s in r.result.get("signals", []) if s["severity"] in ("medium", "high")],
        )
        for r in rows
    ]


@router.get("/drift/reports/{report_id}", response_model=DriftReportOut)
def get_drift_report(report_id: str, db: Session = Depends(get_db)) -> DriftReportOut:
    row = db.get(DriftReport, report_id)
    if row is None:
        raise HTTPException(404, "drift report not found")
    return _full(row)
