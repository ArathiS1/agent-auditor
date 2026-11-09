"""Database-facing insight queries: overview, failure ledger, timeline, feedback, benchmark.

Everything here is read-only except `record_feedback`, which writes one eval row and re-diagnoses the run.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session, selectinload

from ..audit import record
from ..evals.service import get_agent
from ..models import (Agent, AgentVersion, DriftReport, EvalResult, Release, Run, RunDiagnosis, utcnow)
from ..rootcause.classify import TAXONOMY
from ..rootcause.errors import error_kind
from ..rootcause.service import diagnose_one
from .bench import BenchRun, percentile, summarize

MAX_ROWS = 20000

KIND_ADVICE = {
    "rate_limit": "The provider is throttling you. Add retry with backoff, lower the request rate, or raise your quota.",
    "auth": "The API key is wrong, expired or not allowed to use this model. Replace it and restart the server.",
    "timeout": "The call took too long. Raise the timeout or choose a faster model.",
    "context_length": "The input was longer than the model accepts. Trim or summarise the history and documents.",
    "connection": "The network call could not connect. Check the service is running and reachable from this machine.",
    "server_error": "The provider had an internal error. Retry later or configure a fallback model.",
    "not_found": "Something requested does not exist: often a wrong model name or URL. Check CHAT_MODEL and the endpoint.",
    "code_or_input": "A bug or bad input in the agent's own code. Reproduce it with the failing input (make a regression test).",
    "other": "The message says little. Open the run and read the failing step.",
}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _text(value: Any, limit: int = 240) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        s = value
    elif isinstance(value, dict):
        s = str(value.get("text") or value.get("question") or value.get("message") or value.get("content") or value)
    else:
        s = str(value)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _window(hours: int) -> datetime:
    return utcnow() - timedelta(hours=max(1, min(hours, 24 * 90)))


def _agent_id(db: Session, name: str | None) -> str | None:
    return get_agent(db, name).id if name else None


# ------------------------------------------------------------------ timeline
def timeline(db: Session, agent: str | None, hours: int = 24) -> dict[str, Any]:
    since = _window(hours)
    aid = _agent_id(db, agent)
    stmt = select(Run.started_at, Run.status, Run.latency_ms).where(Run.started_at >= since).order_by(Run.started_at).limit(MAX_ROWS)
    if aid:
        stmt = stmt.where(Run.agent_id == aid)
    rows = db.execute(stmt).all()
    span = max(1, min(hours, 24 * 90))
    if span <= 6:
        step = timedelta(minutes=10)
    elif span <= 48:
        step = timedelta(hours=1)
    elif span <= 24 * 14:
        step = timedelta(hours=6)
    else:
        step = timedelta(days=1)
    start = since.replace(second=0, microsecond=0)
    buckets: dict[int, dict[str, Any]] = {}
    for started, status, lat in rows:
        started = _aware(started)
        idx = int((started - _aware(start)) / step)
        b = buckets.setdefault(idx, {"ok": 0, "error": 0, "lat": []})
        b["ok" if status == "ok" else "error"] += 1
        if lat is not None:
            b["lat"].append(lat)
    n = int((_aware(utcnow()) - _aware(start)) / step) + 1
    out = []
    for i in range(n):
        b = buckets.get(i, {"ok": 0, "error": 0, "lat": []})
        out.append({"t": (_aware(start) + step * i).isoformat(), "ok": b["ok"], "error": b["error"], "p50_ms": percentile(b["lat"], 0.5)})
    return {"step_seconds": int(step.total_seconds()), "buckets": out, "truncated": len(rows) >= MAX_ROWS}


# ------------------------------------------------------------------ failures
def _failed_evals(db: Session, run_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not run_ids:
        return out
    stmt = select(EvalResult).where(EvalResult.run_id.in_(run_ids), EvalResult.passed.is_(False))
    for r in db.scalars(stmt):
        out[r.run_id].append({"scorer": r.scorer, "reason": (r.reason or "")[:300]})
    return out


def failures(db: Session, agent: str | None, hours: int = 168, limit: int = 50, environment: str | None = None) -> dict[str, Any]:
    """The newest runs that went wrong: an error, or a failed check. Each comes with its likely cause and what to try."""
    since = _window(hours)
    aid = _agent_id(db, agent)
    limit = max(1, min(limit, 200))

    bad_eval = select(EvalResult.run_id).where(EvalResult.passed.is_(False))
    stmt = (select(Run).options(selectinload(Run.steps), selectinload(Run.agent), selectinload(Run.version))
            .where(Run.started_at >= since, (Run.status == "error") | Run.id.in_(bad_eval))
            .order_by(Run.started_at.desc()).limit(limit))
    if aid:
        stmt = stmt.where(Run.agent_id == aid)
    if environment:
        stmt = stmt.where(Run.environment == environment)
    runs = list(db.scalars(stmt).all())

    diag_rows = {d.run_id: d for d in db.scalars(select(RunDiagnosis).where(RunDiagnosis.run_id.in_([r.id for r in runs])))} if runs else {}
    wrote = False
    for r in runs:  # diagnose what has not been diagnosed yet, so the ledger always names a cause
        if r.id not in diag_rows:
            diagnose_one(db, r.id)
            wrote = True
    if wrote:
        db.commit()
        diag_rows = {d.run_id: d for d in db.scalars(select(RunDiagnosis).where(RunDiagnosis.run_id.in_([r.id for r in runs])))}
    evals = _failed_evals(db, [r.id for r in runs])

    items: list[dict[str, Any]] = []
    cause_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    for r in runs:
        d = diag_rows.get(r.id)
        step_err = next((s for s in r.steps if s.status == "error"), None)
        msg = r.error or (step_err.error if step_err else None)
        kind = error_kind(msg) if msg else None
        cause = d.cause if d and d.status == "failed" else None
        if cause:
            cause_counts[cause] += 1
        if kind:
            kind_counts[kind] += 1
        advice = list(TAXONOMY.get(cause or "", {}).get("advice", []))
        if kind and KIND_ADVICE.get(kind):
            advice.insert(0, KIND_ADVICE[kind])
        items.append({
            "run_id": r.id, "agent": r.agent.name, "version": r.version.label if r.version else None,
            "environment": r.environment, "started_at": _aware(r.started_at).isoformat(), "status": r.status,
            "question": _text(r.input_data), "answer": _text(r.output_data, 300),
            "error": (msg or "")[:400] or None, "error_kind": kind, "failed_step": step_err.name if step_err else (d.step if d else None),
            "cause": cause, "strength": d.strength if d else None, "explanation": d.explanation if d else None,
            "failed_checks": evals.get(r.id, []), "advice": advice[:3],
            "latency_ms": r.latency_ms, "tags": {k: v for k, v in (r.tags or {}).items() if k in ("mode", "bench", "category", "cancelled")},
        })
    return {"count": len(items), "window_hours": hours, "causes": dict(cause_counts.most_common()),
            "error_kinds": dict(kind_counts.most_common()), "items": items}


# ------------------------------------------------------------------ overview
def overview(db: Session, agent: str | None, hours: int = 168) -> dict[str, Any]:
    since = _window(hours)
    day = _window(24)
    aid = _agent_id(db, agent)

    def base(*cols):
        s = select(*cols).where(Run.started_at >= since)
        return s.where(Run.agent_id == aid) if aid else s

    total = db.scalar(base(func.count(Run.id))) or 0
    errors = db.scalar(base(func.count(Run.id)).where(Run.status == "error")) or 0
    lat = [x for (x,) in db.execute(base(Run.latency_ms).where(Run.status == "ok", Run.latency_ms.is_not(None)).limit(MAX_ROWS))]

    ev = select(func.count(EvalResult.id), func.sum(cast(EvalResult.passed, Integer))) \
        .join(Run, Run.id == EvalResult.run_id).where(EvalResult.applicable.is_(True), Run.started_at >= since)
    if aid:
        ev = ev.where(Run.agent_id == aid)
    ev_n, ev_pass = db.execute(ev).one()
    ev_n, ev_pass = ev_n or 0, int(ev_pass or 0)

    err_msgs = db.execute(base(Run.error).where(Run.status == "error", Run.error.is_not(None)).limit(2000)).all()
    kinds = Counter(error_kind(m) for (m,) in err_msgs)

    dstmt = select(RunDiagnosis.cause, func.count(RunDiagnosis.id)).where(RunDiagnosis.status == "failed", RunDiagnosis.started_at >= since).group_by(RunDiagnosis.cause)
    if aid:
        dstmt = dstmt.where(RunDiagnosis.agent_id == aid)
    causes = {c or "unknown": n for c, n in db.execute(dstmt)}
    # runs that went wrong: errored, or answered badly (a failed check, or diagnosed as failed)
    bad_eval = select(EvalResult.run_id).where(EvalResult.passed.is_(False))
    problems = db.scalar(base(func.count(Run.id)).where((Run.status == "error") | Run.id.in_(bad_eval))) or 0

    # per-agent table
    agents = []
    for a in db.scalars(select(Agent).order_by(Agent.name)):
        if aid and a.id != aid:
            continue
        n = db.scalar(select(func.count(Run.id)).where(Run.agent_id == a.id, Run.started_at >= since)) or 0
        e = db.scalar(select(func.count(Run.id)).where(Run.agent_id == a.id, Run.started_at >= since, Run.status == "error")) or 0
        last = db.scalar(select(func.max(Run.started_at)).where(Run.agent_id == a.id))
        active = db.scalar(select(AgentVersion.label).where(AgentVersion.agent_id == a.id, AgentVersion.is_active.is_(True)))
        nver = db.scalar(select(func.count(AgentVersion.id)).where(AgentVersion.agent_id == a.id)) or 0
        d = db.scalar(select(DriftReport).where(DriftReport.agent_id == a.id).order_by(DriftReport.created_at.desc()).limit(1))
        agents.append({"name": a.name, "description": a.description, "runs": n, "errors": e, "error_rate": (e / n) if n else None,
                       "last_run": _aware(last).isoformat() if last else None, "active_version": active, "versions": nver,
                       "drift": d.overall if d else None})

    alerts: list[dict[str, Any]] = []
    recent = base(func.count(Run.id)).where(Run.started_at >= day)
    r_n = db.scalar(recent) or 0
    r_e = db.scalar(recent.where(Run.status == "error")) or 0
    if r_n >= 5 and r_e / r_n >= 0.10:
        alerts.append({"severity": "high", "title": f"{r_e} of the last {r_n} runs in 24 hours failed",
                       "detail": "Open Failures to see the errors and what to change.", "view": "failures"})
    elif r_n and r_e:
        alerts.append({"severity": "medium", "title": f"{r_e} failed run{'s' if r_e != 1 else ''} in the last 24 hours", "detail": "Open Failures for the cause of each.", "view": "failures"})
    if causes:
        top, n = max(causes.items(), key=lambda kv: kv[1])
        alerts.append({"severity": "medium", "title": f"Most common cause: {top.replace('_', ' ')} ({n} runs)",
                       "detail": (TAXONOMY.get(top, {}).get("advice") or [""])[0], "view": "causes"})
    for a in agents:
        if a["drift"] in ("drift", "minor_drift"):
            alerts.append({"severity": "high" if a["drift"] == "drift" else "medium", "title": f"{a['name']}: behaviour has drifted" if a["drift"] == "drift" else f"{a['name']}: minor drift",
                           "detail": "Compare recent runs with the reference period in Drift.", "view": "drift"})
    rstmt = select(Release).where(Release.state == "proposed")
    if aid:
        rstmt = rstmt.where(Release.agent_id == aid)
    for rel in db.scalars(rstmt.limit(5)):
        alerts.append({"severity": "info", "title": f"Release {rel.candidate_label} is waiting for a decision", "detail": "Review the gate result, then approve or reject.", "view": "releases"})
    unevaluated = db.scalar(base(func.count(Run.id)).where(Run.id.not_in(select(EvalResult.run_id)))) or 0
    if unevaluated:
        alerts.append({"severity": "info", "title": f"{unevaluated} run{'s' if unevaluated != 1 else ''} not scored yet", "detail": "Press 'Score now' on the overview to check them.", "view": "overview"})
    order = {"high": 0, "medium": 1, "info": 2}
    alerts.sort(key=lambda a: order[a["severity"]])

    return {"window_hours": hours, "runs": total, "errors": errors, "error_rate": (errors / total) if total else None,
            "problem_runs": problems, "problem_rate": (problems / total) if total else None,
            "evaluated": ev_n, "passed": ev_pass, "pass_rate": (ev_pass / ev_n) if ev_n else None,
            "p50_ms": percentile(lat, 0.5), "p95_ms": percentile(lat, 0.95),
            "error_kinds": dict(kinds.most_common()), "causes": causes, "agents": agents, "alerts": alerts,
            "unevaluated": unevaluated}


# ------------------------------------------------------------------ feedback
def record_feedback(db: Session, *, agent: str, turn_id: str, rating: str, comment: str | None = None) -> dict[str, Any]:
    """Store a thumbs up/down from a person against the run that produced the answer, then re-diagnose it."""
    if rating not in ("up", "down"):
        raise ValueError("rating must be 'up' or 'down'")
    agent_row = get_agent(db, agent)
    run = None
    stmt = select(Run).where(Run.agent_id == agent_row.id).order_by(Run.started_at.desc()).limit(500)
    for candidate in db.scalars(stmt):
        if (candidate.tags or {}).get("turn_id") == turn_id:
            run = candidate
            break
    if run is None:
        raise LookupError("no run with that turn id yet (it may still be on its way; try again in a moment)")
    row = db.scalar(select(EvalResult).where(EvalResult.run_id == run.id, EvalResult.scorer == "human_feedback"))
    if row is None:
        row = EvalResult(run_id=run.id, scorer="human_feedback")
        db.add(row)
    row.applicable, row.score, row.passed = True, 1.0 if rating == "up" else 0.0, rating == "up"
    row.reason = (f"person gave a thumbs {rating}" + (f": {comment.strip()[:300]}" if comment and comment.strip() else ""))
    row.details = {"rating": rating}
    row.evaluated_at = utcnow()
    record(db, "run.feedback", "run", run.id, {"rating": rating}, actor="person")
    db.flush()
    diagnose_one(db, run.id)
    return {"run_id": run.id, "rating": rating}


# ------------------------------------------------------------------ benchmark
def benchmark(db: Session, agent: str, current: str | None = None) -> dict[str, Any]:
    agent_row = get_agent(db, agent)
    stmt = (select(Run).options(selectinload(Run.steps), selectinload(Run.version))
            .where(Run.agent_id == agent_row.id, Run.environment == "test").order_by(Run.started_at.desc()).limit(5000))
    runs = [r for r in db.scalars(stmt) if (r.tags or {}).get("bench")]
    ids = [r.id for r in runs]
    fb = {}
    exp = {}
    if ids:
        for e in db.scalars(select(EvalResult).where(EvalResult.run_id.in_(ids), EvalResult.scorer.in_(("human_feedback", "expectations_met")))):
            if e.scorer == "human_feedback" and e.passed is not None:
                fb[e.run_id] = "up" if e.passed else "down"
            elif e.scorer == "expectations_met" and e.applicable:
                exp[e.run_id] = (e.passed, e.reason)
    rows = []
    for r in runs:
        t = r.tags or {}
        verdict = exp.get(r.id)
        text = r.output_data if isinstance(r.output_data, str) else _text(r.output_data, 4000)
        rows.append(BenchRun(
            run_id=r.id, version=r.version.label if r.version else "unversioned", target=str(t.get("target") or (r.version.label if r.version else "?")),
            question_id=str(t.get("question_id") or r.id), category=str(t.get("category") or "general"),
            question=_text(r.input_data, 400), status=r.status, error=r.error, latency_ms=r.latency_ms,
            passed=(verdict[0] if verdict else None), reason=(verdict[1] if verdict else None), answer=text or "",
            tool_calls=sum(1 for s in r.steps if s.kind in ("tool", "retrieval")), started_at=_aware(r.started_at), feedback=fb.get(r.id)))
    return summarize(rows, current)
