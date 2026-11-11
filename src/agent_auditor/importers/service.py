"""Store imported OpenTelemetry traces as runs. A trace id is the run id, so sending the same trace twice does not duplicate it,
and a trace that arrives in pieces (children first, root last, which is how most exporters batch) is merged into one run."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..audit import record
from ..models import Agent, AgentVersion, Run, Step
from ..versioning import content_hash
from . import otel


def _aware(t: datetime | None) -> datetime | None:
    """SQLite hands datetimes back without a time zone; everything here is UTC."""
    return t if t is None or t.tzinfo else t.replace(tzinfo=timezone.utc)


def _latency(start: datetime | None, end: datetime | None) -> float | None:
    start, end = _aware(start), _aware(end)
    return max(0.0, (end - start).total_seconds() * 1000.0) if start and end else None


def _agent(db: Session, name: str) -> Agent:
    agent = db.scalar(select(Agent).where(Agent.name == name))
    if agent is None:
        agent = Agent(name=name, description="Imported from OpenTelemetry")
        db.add(agent)
        db.flush()
        record(db, "agent.created", "agent", agent.id, {"name": name, "source": "otlp"})
    return agent


def _version(db: Session, agent: Agent, label: str | None) -> AgentVersion | None:
    if label:
        found = db.scalar(select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.label == label))
        if found:
            return found
        config = {"source": "otlp", "label": label}  # the label keeps the content hash unique per label
        version = AgentVersion(agent_id=agent.id, label=label, prompt=None, config=config,
                               content_hash=content_hash(None, config), is_active=False)
        db.add(version)
        db.flush()
        record(db, "version.registered", "agent_version", version.id, {"agent": agent.name, "label": label, "activated": False, "source": "otlp"})
        return version
    return db.scalar(select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.is_active.is_(True)))


def _run_id(db: Session, agent: Agent, trace_id: str) -> str:
    """The trace id, unless another agent already owns that run id (one trace crossing two services)."""
    existing = db.get(Run, trace_id)
    if existing is None or existing.agent_id == agent.id:
        return trace_id
    return hashlib.sha256(f"{agent.id}:{trace_id}".encode()).hexdigest()[:32]


def _step_row(position: int, s: otel.StepDraft) -> Step:
    return Step(position=position, kind=s.kind, name=s.name[:200] or "span", input_data=s.input, output_data=s.output, status=s.status,
                error=s.error, started_at=s.started_at, ended_at=s.ended_at, latency_ms=_latency(s.started_at, s.ended_at),
                attributes=s.attributes)


def _renumber(run: Run) -> None:
    epoch = datetime.max.replace(tzinfo=timezone.utc)

    def key(s: Step):
        return _aware(s.started_at) or epoch

    for i, step in enumerate(sorted(run.steps, key=key)):
        step.position = i


def import_traces(db: Session, payload: Any, *, agent: str | None = None, environment: str | None = None,
                  version: str | None = None) -> dict[str, Any]:
    """Store every trace in an OTLP/JSON request. Caller commits. Returns what was done."""
    spans = otel.parse(payload)
    created = updated = 0
    agents: set[str] = set()
    for draft in otel.build_runs(spans, agent=agent, environment=environment, version=version):
        a = _agent(db, draft.agent[:200])
        agents.add(a.name)
        rid = _run_id(db, a, draft.trace_id)
        run = db.get(Run, rid)
        if run is None:
            ver = _version(db, a, draft.version)
            started = draft.started_at or datetime.now(timezone.utc)
            run = Run(id=rid, agent_id=a.id, version_id=ver.id if ver else None, environment=draft.environment, status=draft.status,
                      input_data=draft.input, output_data=draft.output, error=draft.error, started_at=started, ended_at=draft.ended_at,
                      latency_ms=_latency(started, draft.ended_at), tags=dict(draft.tags, **({} if draft.has_root else {"otel.root_pending": True})))
            for i, s in enumerate(draft.steps):
                run.steps.append(_step_row(i, s))
            db.add(run)
            created += 1
        else:
            have = {(st.attributes or {}).get("otel.span_id") for st in run.steps}
            fresh = [s for s in draft.steps if s.span_id not in have]
            for s in fresh:
                run.steps.append(_step_row(len(run.steps), s))
            root_arrives = draft.has_root and bool((run.tags or {}).get("otel.root_pending"))
            if root_arrives:
                run.input_data, run.output_data = draft.input, draft.output
                run.status, run.error = draft.status, draft.error
                run.started_at = draft.started_at or _aware(run.started_at)
                run.ended_at = draft.ended_at
                run.latency_ms = _latency(run.started_at, run.ended_at)
                tags = {k: v for k, v in (run.tags or {}).items() if k != "otel.root_pending"}
                run.tags = {**tags, **draft.tags}
            elif fresh and not draft.has_root:
                run.ended_at = max([t for t in (_aware(run.ended_at), draft.ended_at) if t], default=_aware(run.ended_at))
                run.latency_ms = _latency(run.started_at, run.ended_at)
            if fresh or root_arrives:
                updated += 1
        db.flush()
        _renumber(run)
        model = next((st.attributes["model"] for st in sorted(run.steps, key=lambda x: x.position) if (st.attributes or {}).get("model")), None)
        if model and "model" not in (run.tags or {}):
            run.tags = {**(run.tags or {}), "model": model}
        if run.input_data is None or run.output_data is None:
            filler = otel.RunDraft(draft.trace_id, a.name, run.environment, None, True, run.started_at, run.ended_at,
                                   input=run.input_data, output=run.output_data,
                                   steps=[otel.StepDraft("", s.kind, s.name, s.input_data, s.output_data, s.status, s.error, s.started_at,
                                                         s.ended_at, s.attributes) for s in sorted(run.steps, key=lambda x: x.position)])
            otel.fill_from_steps(filler)
            run.input_data, run.output_data = filler.input, filler.output
    record(db, "otlp.import", "agent", "all", {"spans": len(spans), "runs_created": created, "runs_updated": updated,
                                                "agents": sorted(agents)})
    return {"spans": len(spans), "runs_created": created, "runs_updated": updated, "agents": sorted(agents)}
