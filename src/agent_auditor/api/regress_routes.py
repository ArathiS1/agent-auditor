from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..regress import service
from .deps import get_db, require_api_key
from .regress_schemas import (
    CaseFromRunIn, CaseFromRunOut, CaseOut, CasePatch, GenerateIn, GenerateOut, ReplayIn, ReplayOut,
)

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


def _wrap(call):
    try:
        return call()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/agents/{name}/tests/generate", response_model=GenerateOut)
def generate_tests(name: str, body: GenerateIn, db: Session = Depends(get_db)) -> dict:
    data = _wrap(lambda: service.generate_cases(db, agent=name, **body.model_dump()))
    db.commit()
    return data


@router.get("/agents/{name}/tests", response_model=list[CaseOut])
def list_tests(
    name: str,
    enabled: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict]:
    return _wrap(lambda: service.list_cases(db, name, enabled=enabled, limit=limit))


@router.post("/runs/{run_id}/to-test", response_model=CaseFromRunOut)
def run_to_test(run_id: str, body: CaseFromRunIn, db: Session = Depends(get_db)) -> dict:
    def go():
        row, created, agent_name = service.case_from_run(
            db, run_id, name=body.name, expectations=body.expectations, redact=body.redact, notes=body.notes
        )
        return {"created": created, "case": service.case_to_dict(row, agent_name)}

    data = _wrap(go)
    db.commit()
    return data


@router.get("/tests/{case_id}", response_model=CaseOut)
def get_test(case_id: str, db: Session = Depends(get_db)) -> dict:
    return _wrap(lambda: service.get_case(db, case_id))


@router.patch("/tests/{case_id}", response_model=CaseOut)
def patch_test(case_id: str, body: CasePatch, db: Session = Depends(get_db)) -> dict:
    data = _wrap(lambda: service.update_case(db, case_id, **body.model_dump(exclude_unset=True)))
    db.commit()
    return data


@router.post("/agents/{name}/replays", response_model=ReplayOut)
def create_replay(name: str, body: ReplayIn, db: Session = Depends(get_db)) -> dict:
    payload = body.model_dump()
    payload["results"] = [r for r in (payload["results"] or [])] or None
    data = _wrap(lambda: service.record_replay(db, agent=name, **payload))
    db.commit()
    return data


@router.get("/agents/{name}/replays", response_model=list[ReplayOut])
def list_replays(name: str, limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db)) -> list[dict]:
    return _wrap(lambda: service.list_replays(db, name, limit))


@router.get("/replays/{replay_id}", response_model=ReplayOut)
def get_replay(replay_id: str, db: Session = Depends(get_db)) -> dict:
    return _wrap(lambda: service.get_replay(db, replay_id))
