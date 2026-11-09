"""Database-facing part of the eval engine: pick runs, score them, store results, aggregate."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from ..audit import record
from ..models import Agent, AgentVersion, EvalResult, Run, utcnow
from .compare import Counts
from .scorers import default_scorer_names, evaluate_run
from .types import RunView, StepView

UNVERSIONED = "(unversioned)"
MAX_BATCH = 500


def run_to_view(run: Run) -> RunView:
    return RunView(
        id=run.id, status=run.status, error=run.error, input=run.input_data, output=run.output_data,
        latency_ms=run.latency_ms, tags=dict(run.tags or {}),
        steps=tuple(
            StepView(
                kind=s.kind, name=s.name, status=s.status, error=s.error, input=s.input_data,
                output=s.output_data, attributes=s.attributes or {}, latency_ms=s.latency_ms,
            )
            for s in run.steps
        ),
    )


@dataclass
class BatchResult:
    evaluated_runs: int = 0
    results_written: int = 0
    scored: int = 0
    not_applicable: int = 0
    scorer_errors: dict[str, str] = field(default_factory=dict)
    scorer_error_count: int = 0
    has_more: bool = False


def _find_agent(db: Session, name: str) -> Agent:
    agent = db.scalar(select(Agent).where(Agent.name == name))
    if agent is None:
        raise LookupError(f"agent '{name}' not found")
    return agent


def _find_version(db: Session, agent: Agent, label: str) -> AgentVersion:
    version = db.scalar(
        select(AgentVersion).where(AgentVersion.agent_id == agent.id, AgentVersion.label == label)
    )
    if version is None:
        raise LookupError(f"version '{label}' not found for agent '{agent.name}'")
    return version


def evaluate_runs(
    db: Session,
    *,
    agent: str | None = None,
    version: str | None = None,
    environment: str | None = None,
    status: str | None = None,
    limit: int = 200,
    only_unevaluated: bool = True,
    scorers: list[str] | None = None,
    params: dict[str, dict[str, Any]] | None = None,
) -> BatchResult:
    """Score a batch of runs and store the results. Caller commits."""
    names = list(scorers) if scorers is not None else default_scorer_names()
    if not names:
        raise ValueError("no scorers selected")
    limit = max(1, min(limit, MAX_BATCH))

    agent_row = _find_agent(db, agent) if agent else None
    version_row = None
    if version:
        if agent_row is None:
            raise ValueError("'version' requires 'agent' (version labels are only unique per agent)")
        version_row = _find_version(db, agent_row, version)

    stmt = select(Run).options(selectinload(Run.steps)).order_by(Run.started_at.desc())
    if agent_row is not None:
        stmt = stmt.where(Run.agent_id == agent_row.id)
    if version_row is not None:
        stmt = stmt.where(Run.version_id == version_row.id)
    if environment:
        stmt = stmt.where(Run.environment == environment)
    if status:
        stmt = stmt.where(Run.status == status)
    if only_unevaluated:
        done = (
            select(EvalResult.run_id)
            .where(EvalResult.scorer.in_(names))
            .group_by(EvalResult.run_id)
            .having(func.count(func.distinct(EvalResult.scorer)) >= len(names))
        )
        stmt = stmt.where(Run.id.not_in(done))

    runs = list(db.scalars(stmt.limit(limit)).all())
    result = BatchResult(has_more=len(runs) == limit)
    if not runs:
        _audit(db, agent_row, result, names, only_unevaluated)
        return result

    existing = {
        (row.run_id, row.scorer): row
        for row in db.scalars(
            select(EvalResult).where(EvalResult.run_id.in_([r.id for r in runs]), EvalResult.scorer.in_(names))
        )
    }

    now = utcnow()
    for run in runs:
        todo = [n for n in names if not (only_unevaluated and (run.id, n) in existing)]
        if not todo:
            continue
        outcomes, errors = evaluate_run(run_to_view(run), todo, params)
        result.evaluated_runs += 1
        for name, message in errors.items():
            result.scorer_error_count += 1
            result.scorer_errors.setdefault(name, message)
        for name, outcome in outcomes.items():
            row = existing.get((run.id, name))
            if row is None:
                row = EvalResult(run_id=run.id, scorer=name)
                db.add(row)
                existing[(run.id, name)] = row
            if outcome is None:
                row.applicable, row.score, row.passed = False, None, None
                row.reason, row.details = "not applicable to this run", {}
                result.not_applicable += 1
            else:
                row.applicable, row.score, row.passed = True, outcome.score, outcome.passed
                row.reason, row.details = outcome.reason[:1000], outcome.details
                result.scored += 1
            row.evaluated_at = now
            result.results_written += 1

    _audit(db, agent_row, result, names, only_unevaluated)
    return result


def _audit(db: Session, agent_row: Agent | None, result: BatchResult, names: list[str], only_unevaluated: bool) -> None:
    record(
        db, "evals.run", "agent", agent_row.id if agent_row else "all",
        {"scorers": names, "only_unevaluated": only_unevaluated, "evaluated_runs": result.evaluated_runs,
         "results_written": result.results_written, "scorer_error_count": result.scorer_error_count},
    )


def _filters(stmt, agent: Agent, environment: str | None, since: datetime | None):
    stmt = stmt.where(Run.agent_id == agent.id)
    if environment:
        stmt = stmt.where(Run.environment == environment)
    if since is not None:
        stmt = stmt.where(Run.started_at >= since)
    return stmt


def _to_counts(n: Any, passed: Any, mean: Any) -> Counts:
    return Counts(int(n or 0), int(passed or 0), float(mean) if mean is not None else None)


def version_counts(
    db: Session, agent: Agent, version_label: str, *, environment: str | None = None, since: datetime | None = None
) -> dict[str, Counts]:
    """Per-scorer applicable/passed counts for one version of an agent."""
    stmt = (
        select(
            EvalResult.scorer,
            func.count(EvalResult.id),
            func.sum(case((EvalResult.passed.is_(True), 1), else_=0)),
            func.avg(EvalResult.score),
        )
        .select_from(EvalResult)
        .join(Run, Run.id == EvalResult.run_id)
        .where(EvalResult.applicable.is_(True))
    )
    stmt = _filters(stmt, agent, environment, since)
    if version_label == UNVERSIONED:
        stmt = stmt.where(Run.version_id.is_(None))
    else:
        version = _find_version(db, agent, version_label)
        stmt = stmt.where(Run.version_id == version.id)
    stmt = stmt.group_by(EvalResult.scorer)
    return {scorer: _to_counts(n, passed, mean) for scorer, n, passed, mean in db.execute(stmt)}


def agent_summary(
    db: Session, agent: Agent, *, environment: str | None = None, since: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """version label -> {runs, evaluated_runs, scorers: {name: Counts}} for one agent."""
    label_of = lambda value: value if value is not None else UNVERSIONED  # noqa: E731

    out: dict[str, dict[str, Any]] = {}

    runs_stmt = (
        select(AgentVersion.label, func.count(Run.id))
        .select_from(Run)
        .outerjoin(AgentVersion, AgentVersion.id == Run.version_id)
    )
    for label, total in db.execute(_filters(runs_stmt, agent, environment, since).group_by(AgentVersion.label)):
        out[label_of(label)] = {"runs": int(total), "evaluated_runs": 0, "scorers": {}}

    evaluated_stmt = (
        select(AgentVersion.label, func.count(func.distinct(EvalResult.run_id)))
        .select_from(EvalResult)
        .join(Run, Run.id == EvalResult.run_id)
        .outerjoin(AgentVersion, AgentVersion.id == Run.version_id)
    )
    for label, total in db.execute(_filters(evaluated_stmt, agent, environment, since).group_by(AgentVersion.label)):
        out.setdefault(label_of(label), {"runs": 0, "evaluated_runs": 0, "scorers": {}})["evaluated_runs"] = int(total)

    scorer_stmt = (
        select(
            AgentVersion.label,
            EvalResult.scorer,
            func.count(EvalResult.id),
            func.sum(case((EvalResult.passed.is_(True), 1), else_=0)),
            func.avg(EvalResult.score),
        )
        .select_from(EvalResult)
        .join(Run, Run.id == EvalResult.run_id)
        .outerjoin(AgentVersion, AgentVersion.id == Run.version_id)
        .where(EvalResult.applicable.is_(True))
    )
    for label, scorer, n, passed, mean in db.execute(
        _filters(scorer_stmt, agent, environment, since).group_by(AgentVersion.label, EvalResult.scorer)
    ):
        out.setdefault(label_of(label), {"runs": 0, "evaluated_runs": 0, "scorers": {}})["scorers"][scorer] = _to_counts(n, passed, mean)
    return out


def get_agent(db: Session, name: str) -> Agent:
    return _find_agent(db, name)
