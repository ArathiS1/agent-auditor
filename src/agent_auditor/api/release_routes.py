from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..release import Conflict, service
from .deps import get_db, require_api_key
from .release_schemas import (
    AuditEventOut, CancelIn, DecideIn, ProposeIn, PromoteIn, ReleaseOut, RollbackIn,
)

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


def _wrap(db: Session, call):
    """Map service errors to HTTP. A Conflict may carry an audit row (a refused approval), so commit it."""
    try:
        return call()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except Conflict as exc:
        db.commit()
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/agents/{name}/releases", response_model=ReleaseOut, status_code=201)
def propose(name: str, body: ProposeIn, db: Session = Depends(get_db)) -> dict:
    data = _wrap(db, lambda: service.propose(
        db, agent=name, candidate=body.candidate, proposed_by=body.proposed_by, notes=body.notes,
        policy=body.policy.model_dump()))
    db.commit()
    return data


@router.get("/agents/{name}/releases", response_model=list[ReleaseOut])
def list_releases(
    name: str, state: str | None = None, limit: int = Query(default=20, ge=1, le=100), db: Session = Depends(get_db),
) -> list[dict]:
    return _wrap(db, lambda: service.list_releases(db, name, state, limit))


@router.get("/releases/{release_id}", response_model=ReleaseOut)
def get_release(release_id: str, db: Session = Depends(get_db)) -> dict:
    return _wrap(db, lambda: service.get_release(db, release_id))


@router.post("/releases/{release_id}/approve", response_model=ReleaseOut)
def decide(release_id: str, body: DecideIn, db: Session = Depends(get_db)) -> dict:
    data = _wrap(db, lambda: service.decide(
        db, release_id, approver=body.approver, decision=body.decision, comment=body.comment))
    db.commit()
    return data


@router.post("/releases/{release_id}/promote", response_model=ReleaseOut)
def promote(release_id: str, body: PromoteIn, db: Session = Depends(get_db)) -> dict:
    result = _wrap(db, lambda: service.promote(db, release_id, actor=body.actor))
    db.commit()  # a refusal is audited too
    if not result["promoted"]:
        raise HTTPException(409, {"message": "the gate is not satisfied", "blockers": result["blockers"]})
    return result["release"]


@router.post("/releases/{release_id}/rollback", response_model=ReleaseOut)
def rollback(release_id: str, body: RollbackIn, db: Session = Depends(get_db)) -> dict:
    data = _wrap(db, lambda: service.rollback(
        db, release_id, actor=body.actor, reason=body.reason, to_version=body.to_version))
    db.commit()
    return data


@router.post("/releases/{release_id}/cancel", response_model=ReleaseOut)
def cancel(release_id: str, body: CancelIn, db: Session = Depends(get_db)) -> dict:
    data = _wrap(db, lambda: service.cancel(db, release_id, actor=body.actor, reason=body.reason))
    db.commit()
    return data


@router.get("/releases/{release_id}/audit", response_model=list[AuditEventOut])
def release_audit(release_id: str, db: Session = Depends(get_db)) -> list[dict]:
    _wrap(db, lambda: service.get_release(db, release_id))  # 404 for unknown ids
    events = service.audit_events(db, entity_id=release_id, limit=500)
    return list(reversed(events))  # oldest first reads like a story


@router.get("/audit", response_model=list[AuditEventOut])
def audit(
    action_prefix: str | None = None, entity_type: str | None = None, entity_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500), db: Session = Depends(get_db),
) -> list[dict]:
    return service.audit_events(
        db, entity_id=entity_id, entity_type=entity_type, action_prefix=action_prefix, limit=limit)
