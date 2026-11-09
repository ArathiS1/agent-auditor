"""Read-mostly endpoints that power the dashboard: overview, failures, timeline, benchmark, gap analysis, feedback."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..insights import gap, service
from ..models import AgentVersion
from .deps import get_db, require_api_key

router = APIRouter(prefix="/v1/insights", dependencies=[Depends(require_api_key)])
feedback_router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


def _wrap(call):
    try:
        return call()
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/overview")
def overview(agent: str | None = None, hours: int = Query(168, ge=1, le=2160), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _wrap(lambda: service.overview(db, agent, hours))


@router.get("/timeline")
def timeline(agent: str | None = None, hours: int = Query(24, ge=1, le=2160), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _wrap(lambda: service.timeline(db, agent, hours))


@router.get("/failures")
def failures(agent: str | None = None, hours: int = Query(168, ge=1, le=2160), limit: int = Query(50, ge=1, le=200),
             environment: str | None = Query(None, pattern="^(production|canary|test)$"), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _wrap(lambda: service.failures(db, agent, hours, limit, environment))


@router.get("/benchmark")
def benchmark(agent: str = "chat-agent", db: Session = Depends(get_db)) -> dict[str, Any]:
    def go() -> dict[str, Any]:
        current = db.scalar(select(AgentVersion.label).join(AgentVersion.agent).where(AgentVersion.is_active.is_(True), AgentVersion.agent.has(name=agent)))
        return service.benchmark(db, agent, current)
    return _wrap(go)


@router.get("/gap-analysis")
def gap_analysis(agent: str = "chat-agent", db: Session = Depends(get_db)) -> dict[str, Any]:
    """Compare the chat's declared features with other assistants. The chat declares them in its registered version config."""
    ours: list[str] | None = None
    rows = db.scalars(select(AgentVersion).where(AgentVersion.agent.has(name=agent)).order_by(AgentVersion.created_at.desc()).limit(20)).all()
    for v in rows:
        feats = (v.config or {}).get("features")
        if isinstance(feats, list):
            ours = [str(f) for f in feats]
            break
    return gap.analyse(ours)


class FeedbackIn(BaseModel):
    agent: str = Field(min_length=1, max_length=200)
    turn_id: str = Field(min_length=1, max_length=100)
    rating: Literal["up", "down"]
    comment: str | None = Field(default=None, max_length=1000)


@feedback_router.post("/feedback")
def feedback(body: FeedbackIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    data = _wrap(lambda: service.record_feedback(db, agent=body.agent, turn_id=body.turn_id, rating=body.rating, comment=body.comment))
    db.commit()
    return data
