"""Database-facing part of drift detection: load two time windows, run the detector, save the report."""
from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from ..audit import record
from ..evals.service import UNVERSIONED, _find_version, get_agent, run_to_view
from ..models import AgentVersion, DriftReport, EvalResult, Run, utcnow
from .detect import detect_drift


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _sanitize(value: Any) -> Any:
    """Make a result safe to store as JSON: no NaN or infinity."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    return value


def _conditions(agent_id: str, start: datetime, end: datetime, environment: str, version_id: str | None) -> list:
    conds = [Run.agent_id == agent_id, Run.started_at >= start, Run.started_at < end, Run.environment == environment]
    if version_id is not None:
        conds.append(Run.version_id == version_id)
    return conds


def _load_window(db: Session, conds: list, max_runs: int):
    total = int(db.scalar(select(func.count(Run.id)).where(*conds)) or 0)
    runs = db.scalars(
        select(Run).options(selectinload(Run.steps)).where(*conds).order_by(Run.started_at.desc()).limit(max_runs)
    ).all()
    return [run_to_view(r) for r in runs], total


def _version_mix(db: Session, conds: list) -> dict[str, int]:
    stmt = (
        select(AgentVersion.label, func.count(Run.id))
        .select_from(Run)
        .outerjoin(AgentVersion, AgentVersion.id == Run.version_id)
        .where(*conds)
        .group_by(AgentVersion.label)
    )
    return {(label if label is not None else UNVERSIONED): int(n) for label, n in db.execute(stmt)}


def _eval_counts(db: Session, conds: list) -> dict[str, tuple[int, int]]:
    stmt = (
        select(EvalResult.scorer, func.count(EvalResult.id), func.sum(case((EvalResult.passed.is_(True), 1), else_=0)))
        .select_from(EvalResult)
        .join(Run, Run.id == EvalResult.run_id)
        .where(EvalResult.applicable.is_(True), *conds)
        .group_by(EvalResult.scorer)
    )
    return {scorer: (int(passed or 0), int(n)) for scorer, n, passed in db.execute(stmt)}


def run_drift_check(
    db: Session,
    *,
    agent: str,
    environment: str = "production",
    version: str | None = None,
    current_hours: float = 24.0,
    reference_hours: float = 168.0,
    as_of: datetime | None = None,
    min_samples: int = 30,
    alpha: float = 0.01,
    correct_multiple: bool = True,
    include_evals: bool = True,
    max_runs: int = 2000,
    save: bool = True,
) -> tuple[dict[str, Any], DriftReport | None]:
    """Compare the current window (the last `current_hours` before `as_of`) with the reference
    window (the `reference_hours` before that). Caller commits."""
    if current_hours <= 0 or reference_hours <= 0:
        raise ValueError("window lengths must be positive")

    agent_row = get_agent(db, agent)
    version_row = _find_version(db, agent_row, version) if version else None
    as_of_utc = _as_utc(as_of) or utcnow()
    cur_start, cur_end = as_of_utc - timedelta(hours=current_hours), as_of_utc
    ref_end = cur_start
    ref_start = ref_end - timedelta(hours=reference_hours)

    version_id = version_row.id if version_row else None
    ref_conds = _conditions(agent_row.id, ref_start, ref_end, environment, version_id)
    cur_conds = _conditions(agent_row.id, cur_start, cur_end, environment, version_id)
    ref_runs, ref_total = _load_window(db, ref_conds, max_runs)
    cur_runs, cur_total = _load_window(db, cur_conds, max_runs)

    extra_rates = None
    if include_evals:
        ref_evals, cur_evals = _eval_counts(db, ref_conds), _eval_counts(db, cur_conds)
        extra_rates = {
            f"eval:{name}": (ref_evals.get(name, (0, 0)), cur_evals.get(name, (0, 0)))
            for name in sorted(set(ref_evals) | set(cur_evals))
        }

    report = detect_drift(
        ref_runs, cur_runs, extra_rates=extra_rates, min_samples=min_samples, alpha=alpha,
        correct_multiple=correct_multiple,
    )

    notes = list(report.notes)
    for label, total, analysed in (("reference", ref_total, len(ref_runs)), ("current", cur_total, len(cur_runs))):
        if total > analysed:
            notes.append(f"{label} window has {total} runs; analysed the newest {analysed}")
    if version is None:
        ref_mix, cur_mix = _version_mix(db, ref_conds), _version_mix(db, cur_conds)
        if set(ref_mix) != set(cur_mix):
            notes.append(
                f"version mix differs between windows (reference {sorted(ref_mix)}, current {sorted(cur_mix)}); "
                "a deployment can look like drift, so check Phase 3 version comparison too"
            )

    data = _sanitize({
        "agent": agent_row.name, "environment": environment, "version": version,
        "as_of": as_of_utc.isoformat(),
        "windows": {
            "reference_start": ref_start.isoformat(), "reference_end": ref_end.isoformat(),
            "current_start": cur_start.isoformat(), "current_end": cur_end.isoformat(),
        },
        "overall": report.overall,
        "reference_runs": report.reference_runs, "current_runs": report.current_runs,
        "reference_total_runs": ref_total, "current_total_runs": cur_total,
        "alpha": alpha, "min_samples": min_samples, "notes": notes,
        "signals": [asdict(s) for s in report.signals],
    })

    row = None
    if save:
        row = DriftReport(
            agent_id=agent_row.id, as_of=as_of_utc, environment=environment, version_label=version,
            overall=report.overall, result=data,
        )
        db.add(row)
        db.flush()
    record(
        db, "drift.check", "agent", agent_row.id,
        {"overall": report.overall, "report_id": row.id if row else None,
         "flagged": [f"{s.name}:{s.severity}" for s in report.signals if s.severity in ("medium", "high")]},
    )
    return data, row


def list_reports(db: Session, agent_name: str, limit: int) -> list[DriftReport]:
    agent_row = get_agent(db, agent_name)
    return list(
        db.scalars(
            select(DriftReport).where(DriftReport.agent_id == agent_row.id)
            .order_by(DriftReport.created_at.desc()).limit(limit)
        )
    )
