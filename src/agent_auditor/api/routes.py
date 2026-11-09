from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from ..audit import record
from ..models import Agent, AgentVersion, Run, Step
from ..schemas import AgentIn, AgentOut, RunDetail, RunIn, RunSummary, StepOut, VersionIn, VersionOut
from ..versioning import content_hash
from .deps import get_db, require_api_key

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])


# ---------- helpers ----------
def _get_or_create_agent(db: Session, name: str, description: str | None = None) -> tuple[Agent, bool]:
    agent = db.scalar(select(Agent).where(Agent.name == name))
    if agent:
        return agent, False
    agent = Agent(name=name, description=description)
    db.add(agent)
    db.flush()
    record(db, "agent.created", "agent", agent.id, {"name": name})
    return agent, True


def _latency_ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() * 1000.0)


def _version_out(v: AgentVersion, agent_name: str) -> VersionOut:
    return VersionOut(
        id=v.id, agent=agent_name, label=v.label, prompt=v.prompt, config=v.config,
        content_hash=v.content_hash, is_active=v.is_active, created_at=v.created_at,
    )


def _summary_fields(run: Run) -> dict:
    return dict(
        id=run.id, agent=run.agent.name, version=run.version.label if run.version else None,
        environment=run.environment, status=run.status, input=run.input_data, output=run.output_data,
        error=run.error, started_at=run.started_at, ended_at=run.ended_at, latency_ms=run.latency_ms,
        tags=run.tags, step_count=len(run.steps),
    )


def _step_out(s: Step) -> StepOut:
    return StepOut(
        id=s.id, position=s.position, kind=s.kind, name=s.name, input=s.input_data, output=s.output_data,
        status=s.status, error=s.error, started_at=s.started_at, ended_at=s.ended_at,
        latency_ms=s.latency_ms, attributes=s.attributes,
    )


# ---------- health ----------
@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------- agents ----------
@router.post("/agents", response_model=AgentOut)
def upsert_agent(body: AgentIn, response: Response, db: Session = Depends(get_db)) -> AgentOut:
    agent, created = _get_or_create_agent(db, body.name, body.description)
    db.commit()
    response.status_code = 201 if created else 200
    return AgentOut(id=agent.id, name=agent.name, description=agent.description, created_at=agent.created_at)


@router.get("/agents", response_model=list[AgentOut])
def list_agents(db: Session = Depends(get_db)) -> list[AgentOut]:
    agents = db.scalars(select(Agent).order_by(Agent.name)).all()
    return [AgentOut(id=a.id, name=a.name, description=a.description, created_at=a.created_at) for a in agents]


# ---------- versions ----------
@router.post("/agents/{name}/versions", response_model=VersionOut)
def register_version(name: str, body: VersionIn, response: Response, db: Session = Depends(get_db)) -> VersionOut:
    agent, _ = _get_or_create_agent(db, name)
    digest = content_hash(body.prompt, body.config)

    existing = db.scalar(
        select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.content_hash == digest)
    )
    if existing:  # idempotent: identical prompt+config is the same version
        db.commit()
        return _version_out(existing, agent.name)

    if db.scalar(select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.label == body.label)):
        raise HTTPException(409, f"label '{body.label}' already exists with different content")

    if body.activate:
        db.execute(update(AgentVersion).where(AgentVersion.agent_id == agent.id).values(is_active=False))
    version = AgentVersion(
        agent_id=agent.id, label=body.label, prompt=body.prompt, config=body.config,
        content_hash=digest, is_active=body.activate,
    )
    db.add(version)
    db.flush()
    record(db, "version.registered", "agent_version", version.id,
           {"agent": agent.name, "label": body.label, "activated": body.activate})
    db.commit()
    response.status_code = 201
    return _version_out(version, agent.name)


@router.get("/agents/{name}/versions", response_model=list[VersionOut])
def list_versions(name: str, db: Session = Depends(get_db)) -> list[VersionOut]:
    agent = db.scalar(select(Agent).where(Agent.name == name))
    if agent is None:
        raise HTTPException(404, f"agent '{name}' not found")
    versions = db.scalars(
        select(AgentVersion).where(AgentVersion.agent_id == agent.id).order_by(AgentVersion.created_at.desc())
    ).all()
    return [_version_out(v, agent.name) for v in versions]


# ---------- runs ----------
@router.post("/runs", response_model=RunDetail, status_code=201)
def ingest_run(body: RunIn, db: Session = Depends(get_db)) -> RunDetail:
    agent, _ = _get_or_create_agent(db, body.agent)

    if body.version:
        version = db.scalar(
            select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.label == body.version)
        )
        if version is None:
            raise HTTPException(404, f"version '{body.version}' is not registered for agent '{body.agent}'")
    else:
        version = db.scalar(
            select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.is_active.is_(True))
        )

    started = body.started_at or datetime.now(timezone.utc)
    run = Run(
        agent_id=agent.id, version_id=version.id if version else None, environment=body.environment,
        status=body.status, input_data=body.input, output_data=body.output, error=body.error,
        started_at=started, ended_at=body.ended_at, latency_ms=_latency_ms(started, body.ended_at),
        tags=body.tags,
    )
    for position, s in enumerate(body.steps):
        run.steps.append(
            Step(
                position=position, kind=s.kind, name=s.name, input_data=s.input, output_data=s.output,
                status=s.status, error=s.error, started_at=s.started_at, ended_at=s.ended_at,
                latency_ms=_latency_ms(s.started_at, s.ended_at), attributes=s.attributes,
            )
        )
    db.add(run)
    db.commit()
    db.refresh(run)
    return RunDetail(**_summary_fields(run), steps=[_step_out(s) for s in run.steps])


@router.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: str, db: Session = Depends(get_db)) -> RunDetail:
    run = db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return RunDetail(**_summary_fields(run), steps=[_step_out(s) for s in run.steps])


@router.get("/runs", response_model=list[RunSummary])
def list_runs(
    agent: str | None = None,
    status: str | None = Query(default=None, pattern="^(ok|error)$"),
    environment: str | None = Query(default=None, pattern="^(production|canary|test)$"),
    version: str | None = None,
    q: str | None = Query(default=None, max_length=200, description="Text to look for in the question, answer or error"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[RunSummary]:
    stmt = select(Run).options(selectinload(Run.steps)).order_by(Run.started_at.desc())
    if agent:
        stmt = stmt.join(Agent).where(Agent.name == agent)
    if status:
        stmt = stmt.where(Run.status == status)
    if environment:
        stmt = stmt.where(Run.environment == environment)
    if version:
        stmt = stmt.join(AgentVersion, Run.version_id == AgentVersion.id).where(AgentVersion.label == version)
    if q:
        needle = q.lower()
        # JSON columns cannot be searched the same way on every database, so filter a newest-first window in Python
        window = db.scalars(stmt.limit(2000)).all()
        hits = [r for r in window if needle in (str(r.input_data) + " " + str(r.output_data) + " " + (r.error or "")).lower()]
        return [RunSummary(**_summary_fields(r)) for r in hits[offset:offset + limit]]
    runs = db.scalars(stmt.limit(limit).offset(offset)).all()
    return [RunSummary(**_summary_fields(r)) for r in runs]
