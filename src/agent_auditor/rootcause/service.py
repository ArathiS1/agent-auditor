"""Database-facing part of root-cause classification: diagnose stored runs, aggregate causes."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..audit import record
from ..evals.service import _find_version, get_agent, run_to_view
from ..models import AgentVersion, EvalResult, Run, RunDiagnosis, utcnow
from .classify import CLASSIFIER_VERSION, TAXONOMY, Diagnosis, classify_run
from .sources import sources_cover, suspect_sources
from .summary import summarize, version_associations

MAX_BATCH = 500
MAX_REPORT_ROWS = 5000
MAX_SOURCE_RUNS = 600  # runs read to look for suspect source documents (newest first)


def _suspect_sources(db: Session, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """Documents that appear in failing runs much more often than in healthy ones. Returns (suspects, note)."""
    picked = rows[:MAX_SOURCE_RUNS]
    status_of = {r["run_id"]: r["status"] for r in picked}
    if not status_of:
        return [], None
    failed, healthy = [], []
    ids = list(status_of)
    for i in range(0, len(ids), 200):
        for run in db.scalars(select(Run).options(selectinload(Run.steps)).where(Run.id.in_(ids[i:i + 200]))):
            (failed if status_of[run.id] == "failed" else healthy).append(run_to_view(run))
    if sources_cover(failed + healthy) == 0:
        return [], "source-document analysis skipped: retrieval steps do not name their documents (add 'source' or 'doc_id' to each retrieved item)"
    return suspect_sources(failed, healthy), None


def diagnosis_to_dict(row: RunDiagnosis) -> dict[str, Any]:
    return {
        "run_id": row.run_id, "version": row.version_label, "environment": row.environment, "status": row.status,
        "cause": row.cause, "strength": row.strength, "subcause": row.subcause, "step": row.step,
        "findings": row.findings or [], "symptoms": row.symptoms or [], "explanation": row.explanation,
        "classifier_version": row.classifier_version, "classified_at": row.classified_at,
    }


def taxonomy() -> list[dict[str, Any]]:
    return [{"cause": c, "meaning": t["meaning"], "advice": t["advice"]} for c, t in TAXONOMY.items()]


def _failed_scorers(db: Session, run_ids: list[str]) -> dict[str, list[str]]:
    if not run_ids:
        return {}
    out: dict[str, list[str]] = {}
    stmt = select(EvalResult.run_id, EvalResult.scorer).where(EvalResult.run_id.in_(run_ids), EvalResult.passed.is_(False))
    for run_id, scorer in db.execute(stmt):
        out.setdefault(run_id, []).append(scorer)
    return out


def _apply(row: RunDiagnosis, run: Run, version_label: str | None, diag: Diagnosis) -> None:
    row.agent_id, row.version_label, row.environment, row.started_at = run.agent_id, version_label, run.environment, run.started_at
    row.status, row.cause, row.strength, row.subcause, row.step = diag.status, diag.primary, diag.strength, diag.subcause, diag.step
    row.findings = [f.as_dict() for f in diag.findings]
    row.symptoms = list(diag.symptoms)
    row.explanation = diag.explanation or None
    row.classifier_version = CLASSIFIER_VERSION
    row.classified_at = utcnow()


def _labels(db: Session, agent_id: str) -> dict[str, str]:
    return {v.id: v.label for v in db.scalars(select(AgentVersion).where(AgentVersion.agent_id == agent_id))}


def diagnose_runs(
    db: Session,
    *,
    agent: str,
    environment: str = "production",
    version: str | None = None,
    since_hours: float | None = None,
    limit: int = 200,
    reclassify: bool = False,
) -> dict[str, Any]:
    """Classify runs that have no diagnosis yet (newest first). Caller commits."""
    agent_row = get_agent(db, agent)
    version_row = _find_version(db, agent_row, version) if version else None
    limit = max(1, min(limit, MAX_BATCH))

    stmt = select(Run).options(selectinload(Run.steps)).where(Run.agent_id == agent_row.id, Run.environment == environment)
    if version_row is not None:
        stmt = stmt.where(Run.version_id == version_row.id)
    if since_hours is not None:
        stmt = stmt.where(Run.started_at >= utcnow() - timedelta(hours=since_hours))
    if not reclassify:
        stmt = stmt.where(Run.id.not_in(select(RunDiagnosis.run_id)))
    runs = list(db.scalars(stmt.order_by(Run.started_at.desc()).limit(limit)).all())

    labels = _labels(db, agent_row.id)
    failed_by_run = _failed_scorers(db, [r.id for r in runs])
    existing = {}
    if reclassify and runs:
        existing = {d.run_id: d for d in db.scalars(select(RunDiagnosis).where(RunDiagnosis.run_id.in_([r.id for r in runs])))}

    by_cause: Counter = Counter()
    failed = 0
    for run in runs:
        diag = classify_run(run_to_view(run), extra_failed=failed_by_run.get(run.id, []))
        row = existing.get(run.id)
        label = labels.get(run.version_id) if run.version_id else None
        if row is None:
            row = RunDiagnosis(run_id=run.id)
            _apply(row, run, label, diag)
            db.add(row)
        else:
            _apply(row, run, label, diag)
        if diag.status == "failed":
            failed += 1
            by_cause[diag.primary or "unknown"] += 1
    db.flush()
    if runs:
        record(db, "rootcause.diagnose", "agent", agent_row.id, {
            "environment": environment, "classified": len(runs), "failed": failed, "reclassify": reclassify,
        })
    return {
        "agent": agent_row.name, "classified": len(runs), "failed": failed, "healthy": len(runs) - failed,
        "has_more": len(runs) == limit, "by_cause": dict(by_cause.most_common()),
    }


def diagnose_one(db: Session, run_id: str) -> dict[str, Any]:
    """Diagnose (or re-diagnose) a single run and store the result. Caller commits."""
    run = db.scalar(select(Run).options(selectinload(Run.steps)).where(Run.id == run_id))
    if run is None:
        raise LookupError("run not found")
    diag = classify_run(run_to_view(run), extra_failed=_failed_scorers(db, [run.id]).get(run.id, []))
    version_label = _labels(db, run.agent_id).get(run.version_id) if run.version_id else None
    row = db.scalar(select(RunDiagnosis).where(RunDiagnosis.run_id == run.id))
    if row is None:
        row = RunDiagnosis(run_id=run.id)
        _apply(row, run, version_label, diag)  # fill the required columns before the row joins the session
        db.add(row)
    else:
        _apply(row, run, version_label, diag)
    db.flush()
    return diagnosis_to_dict(row)


def root_cause_report(
    db: Session,
    *,
    agent: str,
    environment: str = "production",
    since_hours: float | None = None,
    version: str | None = None,
    diagnose_first: bool = True,
    alpha: float = 0.01,
    min_runs: int = 20,
    min_count: int = 5,
    min_effect: float = 0.10,
) -> dict[str, Any]:
    """What fails, why (by cause), where (by step), and whether a cause clusters in one version."""
    agent_row = get_agent(db, agent)
    notes: list[str] = []
    diagnosed_now = 0
    if diagnose_first:
        batch = diagnose_runs(db, agent=agent, environment=environment, version=version, since_hours=since_hours, limit=MAX_BATCH)
        diagnosed_now = batch["classified"]
        if batch["has_more"]:
            notes.append("more undiagnosed runs remain (500 are classified per call); call again to cover them")

    stmt = select(RunDiagnosis).where(RunDiagnosis.agent_id == agent_row.id, RunDiagnosis.environment == environment)
    if version:
        stmt = stmt.where(RunDiagnosis.version_label == version)
    if since_hours is not None:
        stmt = stmt.where(RunDiagnosis.started_at >= utcnow() - timedelta(hours=since_hours))
    found = db.scalars(stmt.order_by(RunDiagnosis.started_at.desc()).limit(MAX_REPORT_ROWS)).all()
    rows = [
        {"run_id": d.run_id, "version": d.version_label, "status": d.status, "cause": d.cause,
         "strength": d.strength, "subcause": d.subcause, "step": d.step}
        for d in found
    ]
    if len(rows) == MAX_REPORT_ROWS:
        notes.append(f"report covers the newest {MAX_REPORT_ROWS} diagnosed runs")
    if any(d.classifier_version != CLASSIFIER_VERSION for d in found):
        notes.append("some diagnoses came from an older classifier version; pass reclassify via /diagnose to refresh them")
    if not rows:
        notes.append("no diagnosed runs in this window; send runs first, or leave diagnose_first on")

    if version:
        associations = {"tested": 0, "alpha_each": None, "associations": [], "note": "version filter set: nothing to compare against"}
    else:
        associations = version_associations(rows, alpha=alpha, min_runs=min_runs, min_count=min_count, min_effect=min_effect)
    suspects, source_note = _suspect_sources(db, rows) if rows else ([], None)
    if source_note:
        notes.append(source_note)
    return {
        "agent": agent_row.name, "environment": environment, "since_hours": since_hours, "version": version,
        "classifier_version": CLASSIFIER_VERSION, "diagnosed_now": diagnosed_now,
        **summarize(rows), "version_associations": associations, "suspect_sources": suspects, "notes": notes,
    }
