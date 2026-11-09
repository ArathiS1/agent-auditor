"""Run the project's own scoring, root-cause and canary code on the runs, in this process, with no server.

This is the same pure code the server calls (agent_auditor.evals, .rootcause, .canary, .drift), fed the same run data.
It lets you check the whole idea quickly and see what to expect from the server run. It does not touch the database,
the API or the dashboard, so only the server run proves those parts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from agent_auditor.canary.checks import CheckResult, Policy, check_causes, check_drift, check_evals, check_latency
from agent_auditor.canary.decide import decide
from agent_auditor.drift.detect import detect_drift
from agent_auditor.evals.compare import Counts
from agent_auditor.evals.scorers import evaluate_run
from agent_auditor.evals.types import RunView, StepView
from agent_auditor.rootcause.classify import _DEFAULT_SCORERS, classify_run

from support_bot import Record


def _ms(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000


def to_view(rec: Record, index: int) -> RunView:
    p = rec.payload
    steps = tuple(
        StepView(kind=s["kind"], name=s["name"], status=s.get("status", "ok"), error=s.get("error"), input=s.get("input"),
                 output=s.get("output"), attributes=s.get("attributes") or {}, latency_ms=_ms(s["started_at"], s["ended_at"]))
        for s in p["steps"]
    )
    return RunView(id=f"r{index}", status=p["status"], error=p.get("error"), input=p["input"], output=p["output"],
                   latency_ms=_ms(p["started_at"], p["ended_at"]), steps=steps)


def diagnose(views: Sequence[RunView]) -> list[dict]:
    out = []
    for v in views:
        d = classify_run(v)
        out.append({"status": d.status, "cause": d.primary, "subcause": d.subcause, "step": d.step})
    return out


def _counts(views: Sequence[RunView]) -> dict[str, Counts]:
    acc: dict[str, list[float]] = {}
    for v in views:
        results, _ = evaluate_run(v, list(_DEFAULT_SCORERS), {})
        for name, r in results.items():
            if r is not None:
                acc.setdefault(name, []).append((1.0 if r.passed else 0.0, r.score))
    return {n: Counts(len(x), int(sum(p for p, _ in x)), sum(s for _, s in x) / len(x)) for n, x in acc.items()}


def canary(baseline: Sequence[RunView], candidate: Sequence[RunView], policy: Policy | None = None) -> dict:
    pol = policy or Policy()
    checks: list[CheckResult] = [check_evals(_counts(baseline), _counts(candidate), pol)]
    checks.append(check_latency([v.latency_ms for v in baseline], [v.latency_ms for v in candidate], pol))
    checks.append(check_causes(diagnose(baseline), diagnose(candidate), pol))
    checks.append(check_drift(detect_drift(list(baseline), list(candidate), min_samples=min(pol.min_baseline_runs, pol.min_canary_runs), alpha=pol.alpha), pol))
    checks.append(check_replay_skipped())
    d = decide(checks, canary_runs=len(candidate), baseline_runs=len(baseline), policy=pol)
    return {"verdict": d.verdict, "reasons": d.reasons, "blocking": d.blocking, "warnings": d.warnings,
            "checks": [{"name": c.name, "status": c.status, "severity": c.severity, "reason": c.reason, "details": c.details} for c in checks]}


def check_replay_skipped() -> CheckResult:
    return CheckResult("replay", "skipped", "none", "no regression replay in this validation")
