"""Database-facing part of canary comparison: gather both sides, run the checks, save the report."""
from __future__ import annotations

from dataclasses import asdict, fields
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import record
from ..drift.detect import detect_drift
from ..drift.service import _conditions, _load_window, _sanitize
from ..evals.service import _find_version, evaluate_runs, get_agent, version_counts
from ..models import CanaryReport, ReplayRun, Run, RunDiagnosis, utcnow
from ..rootcause.service import diagnose_runs
from ..rootcause.change import diff_versions
from .checks import CheckResult, Policy, check_causes, check_change, check_drift, check_evals, check_latency, check_replay
from .decide import decide

MAX_PREP_BATCHES = 10  # each evaluation / diagnosis batch handles up to 500 runs
MAX_DRIFT_RUNS = 2000
POLICY_FIELDS = {f.name for f in fields(Policy)}


def _prepare(db: Session, agent: str, version: str, environment: str, since_hours: float, evaluate: bool, diagnose: bool,
             scorers: list[str] | None = None) -> dict[str, str]:
    """Score and diagnose the runs on one side that have no results yet (bounded work).
    Returns the scorers that raised an error (name -> message), so the report can say they did not count."""
    errors: dict[str, str] = {}
    for _ in range(MAX_PREP_BATCHES):
        res = (evaluate_runs(db, agent=agent, version=version, environment=environment, limit=500, only_unevaluated=True,
                             scorers=scorers) if evaluate else None)
        if res is not None:
            for name, message in res.scorer_errors.items():
                errors.setdefault(name, message)
        if res is None or not res.has_more:
            break
    for _ in range(MAX_PREP_BATCHES):
        out = diagnose_runs(db, agent=agent, environment=environment, version=version, since_hours=since_hours, limit=500) if diagnose else None
        if out is None or not out["has_more"]:
            break
    return errors


def _diagnosis_rows(db: Session, agent_id: str, label: str, environment: str, since) -> list[dict[str, Any]]:
    stmt = select(RunDiagnosis.status, RunDiagnosis.cause).where(
        RunDiagnosis.agent_id == agent_id, RunDiagnosis.version_label == label,
        RunDiagnosis.environment == environment, RunDiagnosis.started_at >= since,
    )
    return [{"status": s, "cause": c} for s, c in db.execute(stmt)]


def _latencies(db: Session, conds: list) -> list[float]:
    return [float(v) for (v,) in db.execute(select(Run.latency_ms).where(*conds, Run.latency_ms.is_not(None)))]


def _latest_replay(db: Session, agent_id: str, label: str) -> dict[str, Any] | None:
    row = db.scalar(
        select(ReplayRun).where(ReplayRun.agent_id == agent_id, ReplayRun.version_label == label)
        .order_by(ReplayRun.created_at.desc()).limit(1)
    )
    if row is None:
        return None
    return {"id": row.id, "verdict": row.verdict, "summary": row.summary or {}, "created_at": row.created_at.isoformat()}


def _report_to_dict(row: CanaryReport) -> dict[str, Any]:
    data = dict(row.result or {})
    data.update({"id": row.id, "created_at": row.created_at})
    return data


def canary_report(
    db: Session,
    *,
    agent: str,
    baseline: str,
    candidate: str,
    baseline_environment: str = "production",
    candidate_environment: str = "canary",
    since_hours: float = 24.0,
    policy: dict[str, Any] | None = None,
    evaluate_first: bool = True,
    diagnose_first: bool = True,
    save: bool = True,
    scorers: list[str] | None = None,
) -> dict[str, Any]:
    """Compare the candidate's runs with the baseline's over the last `since_hours`. Caller commits."""
    if since_hours <= 0:
        raise ValueError("since_hours must be positive")
    if baseline == candidate and baseline_environment == candidate_environment:
        raise ValueError("baseline and candidate are the same version in the same environment")
    unknown = set(policy or {}) - POLICY_FIELDS
    if unknown:
        raise ValueError("unknown policy field(s): " + ", ".join(sorted(unknown)))
    pol = Policy(**(policy or {}))
    pol.validate()

    agent_row = get_agent(db, agent)
    base_v = _find_version(db, agent_row, baseline)
    cand_v = _find_version(db, agent_row, candidate)
    now = utcnow()
    since = now - timedelta(hours=since_hours)
    end = now + timedelta(seconds=1)

    scorer_errors = _prepare(db, agent, baseline, baseline_environment, since_hours, evaluate_first, diagnose_first, scorers)
    for name, message in _prepare(db, agent, candidate, candidate_environment, since_hours, evaluate_first, diagnose_first, scorers).items():
        scorer_errors.setdefault(name, message)
    db.flush()

    base_conds = _conditions(agent_row.id, since, end, baseline_environment, base_v.id)
    cand_conds = _conditions(agent_row.id, since, end, candidate_environment, cand_v.id)
    n_base = int(db.scalar(select(func.count(Run.id)).where(*base_conds)) or 0)
    n_cand = int(db.scalar(select(func.count(Run.id)).where(*cand_conds)) or 0)

    checks: list[CheckResult] = [
        check_evals(
            version_counts(db, agent_row, baseline, environment=baseline_environment, since=since),
            version_counts(db, agent_row, candidate, environment=candidate_environment, since=since),
            pol,
        ),
    ]
    if scorer_errors:  # a scorer that cannot run must not silently drop out of the verdict
        detail = "; ".join(f"{n}: {m}" for n, m in sorted(scorer_errors.items()))
        checks.append(CheckResult(
            "scorers", "warn", "none",
            "some scorers could not run, so they are not part of this verdict (" + detail[:300] + ")",
            {"errors": scorer_errors}))
    if n_base >= pol.min_baseline_runs and n_cand >= pol.min_canary_runs:
        checks.append(check_latency(_latencies(db, base_conds), _latencies(db, cand_conds), pol))
        checks.append(check_causes(
            _diagnosis_rows(db, agent_row.id, baseline, baseline_environment, since),
            _diagnosis_rows(db, agent_row.id, candidate, candidate_environment, since), pol,
        ))
        if pol.include_drift:
            base_views, _ = _load_window(db, base_conds, MAX_DRIFT_RUNS)
            cand_views, _ = _load_window(db, cand_conds, MAX_DRIFT_RUNS)
            report = detect_drift(
                base_views, cand_views, min_samples=min(pol.min_baseline_runs, pol.min_canary_runs), alpha=pol.alpha,
            )
            checks.append(check_drift(report, pol))
    else:
        for name in ("latency", "causes") + (("drift",) if pol.include_drift else ()):
            checks.append(CheckResult(name, "skipped", "none", "not enough live runs on both sides yet"))
    checks.append(check_replay(_latest_replay(db, agent_row.id, candidate), pol))
    version_diff = diff_versions(base_v.prompt, base_v.config, cand_v.prompt, cand_v.config)
    checks.append(check_change(version_diff, next((c for c in checks if c.name == "causes"), None),
                               [c for c in checks if c.name != "scorers"]))

    decision = decide(checks, canary_runs=n_cand, baseline_runs=n_base, policy=pol)
    data = _sanitize({
        "agent": agent_row.name,
        "baseline": {"version": baseline, "environment": baseline_environment, "runs": n_base},
        "candidate": {"version": candidate, "environment": candidate_environment, "runs": n_cand},
        "window": {"since": since.isoformat(), "until": now.isoformat(), "hours": since_hours},
        "verdict": decision.verdict, "reasons": decision.reasons, "blocking": decision.blocking,
        "warnings": decision.warnings,
        "checks": [asdict(c) for c in checks],
        "policy": asdict(pol),
        "caveat": "Repeated checks of the same canary raise the false-alarm rate; "
                  "traffic assigned by a sticky hash is not randomised.",
    })

    row = None
    if save:
        row = CanaryReport(
            agent_id=agent_row.id, baseline_label=baseline, candidate_label=candidate,
            verdict=decision.verdict, result=data,
        )
        db.add(row)
        db.flush()
        data = dict(data, id=row.id, created_at=row.created_at)
    record(db, "canary.evaluate", "agent", agent_row.id, {
        "baseline": baseline, "candidate": candidate, "verdict": decision.verdict,
        "blocking": decision.blocking, "report_id": row.id if row else None,
        "canary_runs": n_cand, "baseline_runs": n_base,
    })
    return data


def list_reports(db: Session, agent_name: str, limit: int = 20, candidate: str | None = None) -> list[dict[str, Any]]:
    agent_row = get_agent(db, agent_name)
    stmt = select(CanaryReport).where(CanaryReport.agent_id == agent_row.id)
    if candidate:
        stmt = stmt.where(CanaryReport.candidate_label == candidate)
    rows = db.scalars(stmt.order_by(CanaryReport.created_at.desc()).limit(max(1, min(limit, 100)))).all()
    return [
        {"id": r.id, "created_at": r.created_at, "agent": agent_row.name, "baseline": r.baseline_label,
         "candidate": r.candidate_label, "verdict": r.verdict,
         "blocking": (r.result or {}).get("blocking", []), "reasons": (r.result or {}).get("reasons", [])}
        for r in rows
    ]


def get_report(db: Session, report_id: str) -> dict[str, Any]:
    row = db.get(CanaryReport, report_id)
    if row is None:
        raise LookupError("canary report not found")
    return _report_to_dict(row)


def latest_report(db: Session, agent_id: str, candidate: str) -> CanaryReport | None:
    """The newest saved canary report for a candidate version (used by approval gates)."""
    return db.scalar(
        select(CanaryReport).where(CanaryReport.agent_id == agent_id, CanaryReport.candidate_label == candidate)
        .order_by(CanaryReport.created_at.desc()).limit(1)
    )
