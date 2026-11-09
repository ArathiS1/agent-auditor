from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..rootcause import service
from .deps import get_db, require_api_key
from .rootcause_schemas import DiagnoseIn, DiagnoseOut, DiagnosisOut, RootCauseIn, RootCauseOut, TaxonomyEntry

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


def _wrap(call):
    try:
        return call()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/root-causes/taxonomy", response_model=list[TaxonomyEntry])
def get_taxonomy() -> list[dict]:
    return service.taxonomy()


@router.post("/agents/{name}/diagnose", response_model=DiagnoseOut)
def diagnose(name: str, body: DiagnoseIn, db: Session = Depends(get_db)) -> dict:
    data = _wrap(lambda: service.diagnose_runs(db, agent=name, **body.model_dump()))
    db.commit()
    return data


@router.get("/runs/{run_id}/diagnosis", response_model=DiagnosisOut)
def get_diagnosis(run_id: str, db: Session = Depends(get_db)) -> dict:
    data = _wrap(lambda: service.diagnose_one(db, run_id))
    db.commit()
    return data


@router.post("/agents/{name}/root-causes", response_model=RootCauseOut)
def root_causes(name: str, body: RootCauseIn, db: Session = Depends(get_db)) -> dict:
    data = _wrap(lambda: service.root_cause_report(db, agent=name, **body.model_dump()))
    db.commit()
    return data
