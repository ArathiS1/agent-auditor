from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..evals import compare_counts, get_scorers, wilson_interval
from ..evals import service
from ..models import EvalResult, Run
from .deps import get_db, require_api_key
from .evals_schemas import (
    CompareOut, EvalBatchOut, EvalResultOut, EvalRunIn, ScorerComparisonOut, ScorerInfo, ScorerStat,
    SummaryOut, VersionSummary,
)

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

Environment = Literal["production", "canary", "test"]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@router.get("/evals/scorers", response_model=list[ScorerInfo])
def list_scorers() -> list[ScorerInfo]:
    return [
        ScorerInfo(name=s.name, description=s.description, defaults=s.defaults, default_enabled=s.default_enabled)
        for s in get_scorers().values()
    ]


@router.post("/evals/run", response_model=EvalBatchOut)
def run_evals(body: EvalRunIn, db: Session = Depends(get_db)) -> EvalBatchOut:
    if body.scorers:
        unknown = [n for n in body.scorers if n not in get_scorers()]
        if unknown:
            raise HTTPException(422, f"unknown scorers: {', '.join(unknown)}")
    try:
        batch = service.evaluate_runs(
            db, agent=body.agent, version=body.version, environment=body.environment, status=body.status,
            limit=body.limit, only_unevaluated=body.only_unevaluated, scorers=body.scorers, params=body.params,
        )
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return EvalBatchOut(**asdict(batch))


@router.get("/runs/{run_id}/evals", response_model=list[EvalResultOut])
def get_run_evals(run_id: str, db: Session = Depends(get_db)) -> list[EvalResultOut]:
    if db.get(Run, run_id) is None:
        raise HTTPException(404, "run not found")
    rows = db.scalars(select(EvalResult).where(EvalResult.run_id == run_id).order_by(EvalResult.scorer)).all()
    return [
        EvalResultOut(
            scorer=r.scorer, applicable=r.applicable, score=r.score, passed=r.passed, reason=r.reason,
            details=r.details, evaluated_at=r.evaluated_at,
        )
        for r in rows
    ]


@router.get("/agents/{name}/eval-summary", response_model=SummaryOut)
def eval_summary(
    name: str,
    environment: Environment | None = None,
    since: datetime | None = None,
    db: Session = Depends(get_db),
) -> SummaryOut:
    try:
        agent = service.get_agent(db, name)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    data = service.agent_summary(db, agent, environment=environment, since=_utc(since))
    versions = []
    for label in sorted(data, key=lambda v: (v == service.UNVERSIONED, v)):
        entry = data[label]
        stats = []
        for scorer, c in sorted(entry["scorers"].items()):
            lo, hi = wilson_interval(c.passed, c.n)
            stats.append(ScorerStat(
                scorer=scorer, n=c.n, passed=c.passed, pass_rate=(c.passed / c.n) if c.n else None,
                ci_low=lo if c.n else None, ci_high=hi if c.n else None, mean_score=c.mean_score,
            ))
        versions.append(VersionSummary(version=label, runs=entry["runs"], evaluated_runs=entry["evaluated_runs"], scorers=stats))
    return SummaryOut(agent=agent.name, versions=versions)


@router.get("/agents/{name}/compare", response_model=CompareOut)
def compare_versions(
    name: str,
    baseline: str = Query(description="Version label of the known-good version"),
    candidate: str = Query(description="Version label under test"),
    baseline_environment: Environment | None = None,
    candidate_environment: Environment | None = None,
    since: datetime | None = None,
    min_samples: int = Query(default=20, ge=1),
    min_effect: float = Query(default=0.05, ge=0.0, le=1.0),
    alpha: float = Query(default=0.05, gt=0.0, lt=1.0),
    correct_multiple: bool = True,
    db: Session = Depends(get_db),
) -> CompareOut:
    if baseline == candidate and baseline_environment == candidate_environment:
        raise HTTPException(422, "compare two different versions, or the same version in different environments")
    try:
        agent = service.get_agent(db, name)
        since_utc = _utc(since)
        base = service.version_counts(db, agent, baseline, environment=baseline_environment, since=since_utc)
        cand = service.version_counts(db, agent, candidate, environment=candidate_environment, since=since_utc)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    result = compare_counts(
        base, cand, min_samples=min_samples, min_effect=min_effect, alpha=alpha, correct_multiple=correct_multiple
    )
    return CompareOut(
        agent=agent.name, baseline=baseline, candidate=candidate, overall_verdict=result.overall_verdict,
        alpha=result.alpha, min_samples=result.min_samples, min_effect=result.min_effect, notes=result.notes,
        scorers=[ScorerComparisonOut(**asdict(row)) for row in result.scorers],
    )
