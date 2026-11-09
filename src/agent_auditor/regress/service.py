"""Database-facing part of failure-to-test conversion and replay grading."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..audit import record
from ..evals.scorers import get_scorers
from ..evals.service import UNVERSIONED, _find_version, evaluate_runs, get_agent, run_to_view
from ..rootcause import classify_run
from ..models import Agent, AgentVersion, EvalResult, RegressionCase, ReplayResult, ReplayRun, Run, utcnow
from .expectations import MISSING, derive_expectations, grade_case, summarize_replay, validate_expectations
from .fingerprint import (
    case_name, failure_info, fingerprint, input_tokens, plan_cases, redact_value,
)

MAX_KNOWN_CASES = 5000
MAX_MERGED_IDS = 1000
MAX_TEST_RUN_SCAN = 5000


# ---------------------------------------------------------------- output shapes
def case_to_dict(row: RegressionCase, agent_name: str) -> dict[str, Any]:
    return {
        "id": row.id, "agent": agent_name, "name": row.name, "enabled": row.enabled, "created_by": row.created_by,
        "created_at": row.created_at, "last_seen_at": row.last_seen_at, "fingerprint": row.fingerprint,
        "source_run_id": row.source_run_id, "origin_version": row.origin_version, "environment": row.environment,
        "input": row.input_data, "reference_output": row.reference_output, "expectations": row.expectations or [],
        "failure": row.failure or {}, "occurrences": row.occurrences, "notes": row.notes,
    }


def replay_to_dict(row: ReplayRun, agent_name: str, *, with_results: bool = True) -> dict[str, Any]:
    return {
        "id": row.id, "agent": agent_name, "version": row.version_label, "label": row.label, "notes": row.notes,
        "created_at": row.created_at, "verdict": row.verdict, "summary": row.summary or {},
        "results": [
            {"case_id": r.case_id, "run_id": r.run_id, "outcome": r.outcome, "previous_outcome": r.previous_outcome,
             "checks": r.checks or []}
            for r in sorted(row.results, key=lambda r: r.case_id)
        ] if with_results else [],
    }


# ---------------------------------------------------------------- failures of a run
def _failed_by_run(db: Session, run_ids: list[str], scorers: list[str] | None) -> dict[str, list[dict[str, Any]]]:
    if not run_ids:
        return {}
    stmt = select(EvalResult).where(EvalResult.run_id.in_(run_ids), EvalResult.passed.is_(False))
    if scorers:
        stmt = stmt.where(EvalResult.scorer.in_(scorers))
    out: dict[str, list[dict[str, Any]]] = {}
    for row in db.scalars(stmt.order_by(EvalResult.scorer)):
        out.setdefault(row.run_id, []).append({"scorer": row.scorer, "reason": row.reason, "details": row.details or {}})
    return out


def _is_replay_run(run: Run) -> bool:
    return bool((run.tags or {}).get("replay_token"))


def _version_label(db: Session, run: Run) -> str | None:
    if run.version_id is None:
        return None
    version = db.get(AgentVersion, run.version_id)
    return version.label if version else None


def _known_cases(db: Session, agent_id: str) -> tuple[list[dict[str, Any]], dict[str, RegressionCase]]:
    rows = db.scalars(
        select(RegressionCase).where(RegressionCase.agent_id == agent_id)
        .order_by(RegressionCase.created_at.desc()).limit(MAX_KNOWN_CASES)
    ).all()
    known = [
        {"key": r.id, "fingerprint": r.fingerprint, "tokens": input_tokens(r.input_data),
         "scorers": frozenset((r.failure or {}).get("scorers") or [])}
        for r in rows
    ]
    return known, {r.id: r for r in rows}


def _represented_run_ids(rows: dict[str, RegressionCase]) -> set[str]:
    seen: set[str] = set()
    for r in rows.values():
        if r.source_run_id:
            seen.add(r.source_run_id)
        seen.update(r.merged_run_ids or [])
    return seen


def _with_cause(info: dict[str, Any], view, failed: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach the root-cause classifier's verdict to a case's failure record."""
    diag = classify_run(view, extra_failed=[f["scorer"] for f in failed])
    return {**info, "cause": diag.primary, "cause_strength": diag.strength, "cause_subcause": diag.subcause}


# ---------------------------------------------------------------- generate cases from failures
def generate_cases(
    db: Session,
    *,
    agent: str,
    environment: str = "production",
    since_hours: float | None = None,
    scorers: list[str] | None = None,
    limit: int = 50,
    include_weak: bool = False,
    similarity: float = 0.8,
    evaluate_first: bool = True,
    redact: bool = False,
    dry_run: bool = False,
    max_scan: int = 1000,
) -> dict[str, Any]:
    """Turn failed runs into regression cases. Caller commits.

    Failures are runs with at least one failed eval result (so run evals first, or leave
    `evaluate_first` on). Failures that repeat an existing case, or each other, are merged into it.
    """
    agent_row = get_agent(db, agent)
    notes: list[str] = []
    evaluated = 0
    if evaluate_first:
        batch = evaluate_runs(db, agent=agent, environment=environment, limit=500, scorers=scorers)
        evaluated = batch.evaluated_runs
        if batch.has_more:
            notes.append("more unevaluated runs remain; call /v1/evals/run again, or generate again, to cover them")
        if batch.scorer_error_count:
            notes.append(f"{batch.scorer_error_count} scorer errors while evaluating; see /v1/evals/run for details")

    failed_run_ids = select(EvalResult.run_id).where(EvalResult.passed.is_(False))
    if scorers:
        failed_run_ids = failed_run_ids.where(EvalResult.scorer.in_(scorers))
    stmt = (
        select(Run).options(selectinload(Run.steps))
        .where(Run.agent_id == agent_row.id, Run.environment == environment, Run.id.in_(failed_run_ids))
    )
    if since_hours is not None:
        stmt = stmt.where(Run.started_at >= utcnow() - timedelta(hours=since_hours))
    runs = [r for r in db.scalars(stmt.order_by(Run.started_at.desc()).limit(max_scan)).all() if not _is_replay_run(r)]
    failed = _failed_by_run(db, [r.id for r in runs], scorers)

    known, known_rows = _known_cases(db, agent_row.id)
    represented = _represented_run_ids(known_rows)

    candidates: list[dict[str, Any]] = []
    by_key: dict[str, Run] = {}
    skipped = {"already_covered": 0, "weak_only": 0}
    for run in runs:
        if run.id in represented:
            skipped["already_covered"] += 1
            continue
        expectations = derive_expectations(failed.get(run.id, []), get_scorers(), include_weak=include_weak)
        if not expectations:
            skipped["weak_only"] += 1
            continue
        stored_input = redact_value(run.input_data) if redact else run.input_data
        view = run_to_view(run)
        info = _with_cause(failure_info(view, failed.get(run.id, [])), view, failed.get(run.id, []))
        candidates.append({
            "key": run.id, "fingerprint": fingerprint(stored_input), "tokens": input_tokens(stored_input),
            "scorers": frozenset(info["scorers"]), "run": run, "input": stored_input, "info": info,
            "expectations": expectations,
        })
        by_key[run.id] = run

    new, merged = plan_cases(candidates, known, similarity)
    truncated = max(0, len(new) - limit)
    new = new[:limit]
    if truncated:
        notes.append(f"{truncated} more distinct failures found than 'limit'; raise the limit to keep them")

    new_rows: dict[str, RegressionCase] = {}
    for cand in new:
        run: Run = cand["run"]
        row = RegressionCase(
            agent_id=agent_row.id, name=case_name(cand["info"], cand["input"]), created_by="auto",
            fingerprint=cand["fingerprint"], source_run_id=run.id, origin_version=_version_label(db, run),
            environment=run.environment, input_data=cand["input"],
            reference_output=redact_value(run.output_data) if redact else run.output_data,
            expectations=cand["expectations"], failure=cand["info"], merged_run_ids=[], occurrences=1,
            last_seen_at=run.started_at, enabled=True,
        )
        new_rows[cand["key"]] = row
        if not dry_run:
            db.add(row)

    targets: dict[str, RegressionCase] = {**known_rows, **new_rows}
    merged_count = 0
    for cand, target_key in merged:
        target = targets.get(target_key)
        if target is None:  # a candidate cut off by 'limit'; nothing to merge into
            continue
        merged_count += 1
        if dry_run and target_key in known_rows:
            continue  # a preview must not change stored cases
        ids = list(target.merged_run_ids or [])
        if len(ids) < MAX_MERGED_IDS:
            ids.append(cand["key"])
        target.merged_run_ids = ids
        target.occurrences = 1 + len(ids)
        started = by_key[cand["key"]].started_at
        if target.last_seen_at is None or started > target.last_seen_at:
            target.last_seen_at = started

    if dry_run and evaluated:
        notes.append("dry run: no test cases were saved, but the unevaluated runs were scored and those results are kept")
    if not dry_run:
        db.flush()
    record(db, "tests.generate", "agent", agent_row.id, {
        "environment": environment, "created": 0 if dry_run else len(new_rows), "merged": merged_count,
        "dry_run": dry_run, "evaluated_runs": evaluated, "redact": redact,
    })
    return {
        "agent": agent_row.name, "dry_run": dry_run, "scanned_failed_runs": len(runs), "evaluated_runs": evaluated,
        "created": [case_to_dict(r, agent_row.name) for r in new_rows.values()],
        "merged_duplicates": merged_count, "skipped": skipped, "notes": notes,
    }


# ---------------------------------------------------------------- pin one run as a test
def case_from_run(
    db: Session,
    run_id: str,
    *,
    name: str | None = None,
    expectations: list[dict[str, Any]] | None = None,
    redact: bool = False,
    notes: str | None = None,
    include_weak: bool = True,
) -> tuple[RegressionCase, bool, str]:
    """Create a case from one chosen run. Returns (row, created, agent_name). Caller commits.

    Without `expectations`, they come from the run's failed eval results (the run is scored first
    if it has none). A run that passed everything needs explicit expectations."""
    run = db.scalar(select(Run).options(selectinload(Run.steps)).where(Run.id == run_id))
    if run is None:
        raise LookupError("run not found")
    agent_row = run.agent
    if _is_replay_run(run):
        raise ValueError("this run is itself a replay; pick the original production run")
    if not db.scalar(select(func.count(EvalResult.id)).where(EvalResult.run_id == run.id)):
        evaluate_runs(db, agent=agent_row.name, environment=run.environment, limit=500)
        if not db.scalar(select(func.count(EvalResult.id)).where(EvalResult.run_id == run.id)):
            if expectations is None:
                raise ValueError("this run has not been scored yet; call /v1/evals/run first, or supply 'expectations'")
    failed = _failed_by_run(db, [run.id], None).get(run.id, [])

    if expectations is None:
        exp = derive_expectations(failed, get_scorers(), include_weak=include_weak)
        if not exp:
            raise ValueError("this run has no failed evals to derive expectations from; supply 'expectations'")
    else:
        exp = validate_expectations(expectations, list(get_scorers()))

    stored_input = redact_value(run.input_data) if redact else run.input_data
    fp = fingerprint(stored_input)
    existing = db.scalar(select(RegressionCase).where(RegressionCase.agent_id == agent_row.id, RegressionCase.fingerprint == fp))
    if existing is not None:
        return existing, False, agent_row.name
    view = run_to_view(run)
    info = _with_cause(failure_info(view, failed), view, failed)
    row = RegressionCase(
        agent_id=agent_row.id, name=(name or case_name(info, stored_input))[:200], created_by="manual", fingerprint=fp,
        source_run_id=run.id, origin_version=_version_label(db, run), environment=run.environment,
        input_data=stored_input, reference_output=redact_value(run.output_data) if redact else run.output_data,
        expectations=exp, failure=info, merged_run_ids=[], occurrences=1, last_seen_at=run.started_at, notes=notes,
    )
    db.add(row)
    db.flush()
    record(db, "tests.create", "regression_case", row.id, {"source_run_id": run.id, "manual": True})
    return row, True, agent_row.name


# ---------------------------------------------------------------- list / update
def list_cases(db: Session, agent: str, *, enabled: bool | None = None, limit: int = 100) -> list[dict[str, Any]]:
    agent_row = get_agent(db, agent)
    stmt = select(RegressionCase).where(RegressionCase.agent_id == agent_row.id)
    if enabled is not None:
        stmt = stmt.where(RegressionCase.enabled.is_(enabled))
    rows = db.scalars(stmt.order_by(RegressionCase.created_at.desc(), RegressionCase.id).limit(limit)).all()
    return [case_to_dict(r, agent_row.name) for r in rows]


def get_case(db: Session, case_id: str) -> dict[str, Any]:
    row = db.get(RegressionCase, case_id)
    if row is None:
        raise LookupError("test case not found")
    return case_to_dict(row, row.agent.name)


def update_case(
    db: Session, case_id: str, *, enabled: bool | None = None, name: str | None = None, notes: str | None = None,
    expectations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Disable/enable, rename, annotate or replace the expectations of a case. Cases are never
    deleted (the audit trail keeps them); disable one instead. Caller commits."""
    row = db.get(RegressionCase, case_id)
    if row is None:
        raise LookupError("test case not found")
    changed: dict[str, Any] = {}
    if enabled is not None and enabled != row.enabled:
        row.enabled = enabled
        changed["enabled"] = enabled
    if name is not None:
        row.name = name[:200]
        changed["name"] = True
    if notes is not None:
        row.notes = notes
        changed["notes"] = True
    if expectations is not None:
        row.expectations = validate_expectations(expectations, list(get_scorers()))
        changed["expectations"] = len(row.expectations)
    if changed:
        record(db, "tests.update", "regression_case", row.id, changed)
    return case_to_dict(row, row.agent.name)


# ---------------------------------------------------------------- replay grading
def record_replay(
    db: Session,
    *,
    agent: str,
    version: str | None = None,
    token: str | None = None,
    results: list[dict[str, str]] | None = None,
    case_ids: list[str] | None = None,
    label: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Grade replayed runs against the agent's enabled test cases and store the outcome.

    The agent itself runs in the caller's process (the SDK's `replay_tests` does this); the server
    only grades. Runs are matched to cases either by `token` (the SDK tags each replay run with a
    token and its case id) or by explicit `results` [{case_id, run_id}]. Cases with no run are
    recorded as 'missing', which stops the verdict from being 'pass'. Caller commits.
    """
    agent_row = get_agent(db, agent)
    if version is not None and version != UNVERSIONED:
        _find_version(db, agent_row, version)  # LookupError -> 404
    if not token and not results:
        raise ValueError("provide 'token' (from the SDK) or 'results' ([{case_id, run_id}])")

    stmt = select(RegressionCase).where(RegressionCase.agent_id == agent_row.id, RegressionCase.enabled.is_(True))
    if case_ids:
        stmt = stmt.where(RegressionCase.id.in_(case_ids))
    cases = list(db.scalars(stmt.order_by(RegressionCase.created_at, RegressionCase.id)).all())
    if case_ids:
        missing_ids = sorted(set(case_ids) - {c.id for c in cases})
        if missing_ids:
            raise ValueError(f"unknown or disabled test case ids for this agent: {', '.join(missing_ids[:5])}")
    if not cases:
        raise ValueError("this agent has no enabled test cases; generate some first")
    case_by_id = {c.id: c for c in cases}

    run_for: dict[str, Run] = {}
    if token:
        stmt_runs = (
            select(Run).where(Run.agent_id == agent_row.id, Run.environment == "test")
            .order_by(Run.started_at.desc()).limit(MAX_TEST_RUN_SCAN)
        )
        for r in db.scalars(stmt_runs):
            tags = r.tags or {}
            cid = tags.get("case_id")
            if tags.get("replay_token") == token and cid in case_by_id and cid not in run_for:
                run_for[cid] = r
    for item in results or []:
        cid, rid = item.get("case_id"), item.get("run_id")
        if cid not in case_by_id:
            raise ValueError(f"result refers to a test case that is not part of this replay: {cid}")
        r = db.get(Run, rid) if rid else None
        if r is None:
            raise ValueError(f"result refers to an unknown run: {rid}")
        if r.agent_id != agent_row.id:
            raise ValueError(f"run {rid} belongs to a different agent")
        run_for[cid] = r

    if version is not None:
        expected = None if version == UNVERSIONED else version
        wrong = {cid: _version_label(db, r) for cid, r in run_for.items() if _version_label(db, r) != expected}
        if wrong:
            cid, got = next(iter(wrong.items()))
            raise ValueError(
                f"{len(wrong)} replay run(s) were recorded under a different version than '{version}' "
                f"(e.g. case {cid} ran as '{got}'); register the version and replay with it"
            )

    previous: dict[str, str | None] = {}
    prev_rows = db.execute(
        select(ReplayResult.case_id, ReplayResult.outcome)
        .join(ReplayRun, ReplayRun.id == ReplayResult.replay_id)
        .where(ReplayRun.agent_id == agent_row.id, ReplayResult.case_id.in_(list(case_by_id)), ReplayResult.outcome != MISSING)
        .order_by(ReplayRun.created_at.desc()).limit(20_000)
    )
    for cid, outcome in prev_rows:
        previous.setdefault(cid, outcome)

    outcomes: dict[str, str] = {}
    graded: dict[str, tuple[str | None, list[dict[str, Any]]]] = {}
    for case in cases:
        run = run_for.get(case.id)
        if run is None:
            outcomes[case.id] = MISSING
            graded[case.id] = (None, [])
            continue
        grade = grade_case(case.expectations or [], run_to_view(run))
        outcomes[case.id] = grade.outcome
        graded[case.id] = (run.id, [c.as_dict() for c in grade.checks])

    summary = summarize_replay(outcomes, previous)
    row = ReplayRun(
        agent_id=agent_row.id, version_label=version, label=label, notes=notes,
        verdict=summary["verdict"], summary=summary,
    )
    for cid, outcome in outcomes.items():
        run_id, checks = graded[cid]
        row.results.append(ReplayResult(case_id=cid, run_id=run_id, outcome=outcome,
                                        previous_outcome=previous.get(cid), checks=checks))
    db.add(row)
    db.flush()
    record(db, "replay.completed", "agent", agent_row.id, {
        "replay_id": row.id, "version": version, "verdict": summary["verdict"], "total": summary["total"],
        "failed": summary["failed"], "newly_failing": len(summary["newly_failing"]),
    })
    return replay_to_dict(row, agent_row.name)


def list_replays(db: Session, agent: str, limit: int = 20) -> list[dict[str, Any]]:
    agent_row = get_agent(db, agent)
    rows = db.scalars(
        select(ReplayRun).where(ReplayRun.agent_id == agent_row.id).order_by(ReplayRun.created_at.desc()).limit(limit)
    ).all()
    return [replay_to_dict(r, agent_row.name, with_results=False) for r in rows]


def get_replay(db: Session, replay_id: str) -> dict[str, Any]:
    row = db.get(ReplayRun, replay_id)
    if row is None:
        raise LookupError("replay not found")
    return replay_to_dict(row, _agent_name(db, row.agent_id))


def _agent_name(db: Session, agent_id: str) -> str:
    agent = db.get(Agent, agent_id)
    return agent.name if agent else ""
