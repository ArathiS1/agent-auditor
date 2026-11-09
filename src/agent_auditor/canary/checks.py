"""The individual canary checks. All pure: they take plain data and return a CheckResult.

Every check answers one question about the candidate versus the baseline and says how sure it is:
  status   pass | warn | fail | skipped
  severity for a fail: "major" (the candidate should be rolled back) or "minor" (hold, investigate)

Nothing here is a probability of "the candidate is bad". These are significance tests plus effect-size
thresholds, so they can be wrong in both directions, and looking at the same canary repeatedly
(peeking) raises the false-alarm rate. See the README for how to use them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..drift.detect import DriftReport
from ..drift.stats import ks_two_sample, quantile
from ..evals.compare import Counts, compare_counts
from ..evals.stats import fisher_exact_two_sided


@dataclass(frozen=True)
class Policy:
    """Thresholds for the canary decision. Defaults are conservative starting points, not laws."""

    min_canary_runs: int = 30
    min_baseline_runs: int = 30
    min_eval_samples: int = 20  # per scorer, on each side
    min_effect: float = 0.05  # smallest pass-rate / cause-rate change worth reporting
    rollback_effect: float = 0.10  # a regression this large is a rollback, smaller is a hold
    alpha: float = 0.01  # stricter than the Phase 3 default because canaries get checked repeatedly
    latency_warn_ratio: float = 1.3
    latency_fail_ratio: float = 2.0
    latency_rollback_ratio: float = 4.0
    require_replay: bool = False
    replay_rollback_min: int = 2  # newly failing cases that force a rollback ...
    replay_rollback_share: float = 0.10  # ... or this share of the suite, whichever is larger
    warnings_block: bool = False  # treat warnings as a hold
    include_drift: bool = True

    def validate(self) -> None:
        if self.min_canary_runs < 1 or self.min_baseline_runs < 1:
            raise ValueError("minimum run counts must be at least 1")
        if not 0 < self.alpha < 1:
            raise ValueError("alpha must be between 0 and 1")
        if not 0 <= self.min_effect <= self.rollback_effect <= 1:
            raise ValueError("need 0 <= min_effect <= rollback_effect <= 1")
        if not 1 <= self.latency_warn_ratio <= self.latency_fail_ratio <= self.latency_rollback_ratio:
            raise ValueError("need 1 <= latency_warn_ratio <= latency_fail_ratio <= latency_rollback_ratio")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # pass | warn | fail | skipped
    severity: str  # none | minor | major
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


def _skipped(name: str, reason: str, **details: Any) -> CheckResult:
    return CheckResult(name, "skipped", "none", reason, dict(details))


# --------------------------------------------------------------------------- evals
def check_evals(baseline: Mapping[str, Counts], canary: Mapping[str, Counts], policy: Policy) -> CheckResult:
    cmp = compare_counts(
        dict(baseline), dict(canary), min_samples=policy.min_eval_samples, min_effect=policy.min_effect,
        alpha=policy.alpha,
    )
    rows = [
        {"scorer": r.scorer, "verdict": r.verdict, "baseline_rate": r.baseline_pass_rate,
         "canary_rate": r.candidate_pass_rate, "delta": r.delta, "p_value": r.p_value, "reason": r.reason}
        for r in cmp.scorers
    ]
    details = {"overall": cmp.overall_verdict, "scorers": rows, "notes": cmp.notes}
    if cmp.overall_verdict == "insufficient_data":
        return CheckResult("evals", "skipped", "none",
                           "no scorer has enough evaluated runs on both sides; run evaluation first", details)
    regressions = [r for r in cmp.scorers if r.verdict == "regression"]
    if regressions:
        worst = min(regressions, key=lambda r: r.delta if r.delta is not None else 0.0)
        major = abs(worst.delta or 0.0) >= policy.rollback_effect
        names = ", ".join(r.scorer for r in regressions)
        return CheckResult(
            "evals", "fail", "major" if major else "minor",
            f"pass rate regressed on {names}; worst is {worst.scorer} ({worst.reason})", details,
        )
    inconclusive = [r.scorer for r in cmp.scorers if r.verdict == "inconclusive"]
    if inconclusive:
        return CheckResult("evals", "warn", "none",
                           "pass rate moved but not significantly on " + ", ".join(inconclusive)
                           + "; more canary traffic would settle it", details)
    return CheckResult("evals", "pass", "none", "no significant pass-rate regression", details)


# --------------------------------------------------------------------------- drift
def check_drift(report: DriftReport | None, policy: Policy) -> CheckResult:
    if report is None:
        return _skipped("drift", "drift check not run")
    flagged = [f"{s.name}:{s.severity}" for s in report.signals if s.severity in ("medium", "high")]
    details = {"overall": report.overall, "flagged": flagged, "notes": list(report.notes)}
    if report.overall == "insufficient_data":
        return _skipped("drift", "not enough runs on both sides for a drift comparison", **details)
    if report.overall == "drift":
        return CheckResult(
            "drift", "warn", "none",
            "canary traffic or behaviour differs from baseline on " + ", ".join(flagged)
            + "; the difference may come from routing rather than the candidate", details,
        )
    return CheckResult("drift", "pass", "none", f"drift verdict: {report.overall}", details)


# --------------------------------------------------------------------------- latency
def check_latency(baseline_ms: Sequence[float], canary_ms: Sequence[float], policy: Policy) -> CheckResult:
    b = sorted(float(x) for x in baseline_ms if x is not None)
    c = sorted(float(x) for x in canary_ms if x is not None)
    if len(b) < policy.min_baseline_runs or len(c) < policy.min_canary_runs:
        return _skipped("latency", f"need latency for at least {policy.min_baseline_runs} baseline and "
                        f"{policy.min_canary_runs} canary runs (have {len(b)} and {len(c)})")
    d, p = ks_two_sample(b, c)
    p50_b, p50_c = quantile(b, 0.5), quantile(c, 0.5)
    p95_b, p95_c = quantile(b, 0.95), quantile(c, 0.95)
    ratios = {}
    for name, old, new in (("p50", p50_b, p50_c), ("p95", p95_b, p95_c)):
        ratios[name] = (new / old) if old > 0 else None
    usable = [r for r in ratios.values() if r is not None]
    ratio = max(usable) if usable else None
    details = {"baseline": {"n": len(b), "p50": p50_b, "p95": p95_b}, "canary": {"n": len(c), "p50": p50_c, "p95": p95_c},
               "ratios": ratios, "ks_d": d, "p_value": p}
    if ratio is None:
        return _skipped("latency", "baseline latency is zero, cannot form a ratio", **details)
    if p >= policy.alpha or ratio < policy.latency_warn_ratio:
        return CheckResult("latency", "pass", "none",
                           f"latency ratio {ratio:.2f}x (KS p={p:.3g}) is within the allowed range", details)
    msg = f"latency is {ratio:.2f}x baseline (p50 {p50_b:.0f}->{p50_c:.0f} ms, p95 {p95_b:.0f}->{p95_c:.0f} ms, KS p={p:.3g})"
    if ratio >= policy.latency_rollback_ratio:
        return CheckResult("latency", "fail", "major", msg, details)
    if ratio >= policy.latency_fail_ratio:
        return CheckResult("latency", "fail", "minor", msg, details)
    return CheckResult("latency", "warn", "none", msg, details)


# --------------------------------------------------------------------------- causes
def check_causes(
    baseline_rows: Sequence[Mapping[str, Any]], canary_rows: Sequence[Mapping[str, Any]], policy: Policy,
    *, min_count: int = 3,
) -> CheckResult:
    """Compare how often each root cause appears per run, canary against baseline."""
    nb, nc = len(baseline_rows), len(canary_rows)
    if nb < policy.min_baseline_runs or nc < policy.min_canary_runs:
        return _skipped("causes", f"need diagnosed runs on both sides (baseline {nb}, canary {nc})")
    kb: dict[str, int] = {}
    kc: dict[str, int] = {}
    for rows, acc in ((baseline_rows, kb), (canary_rows, kc)):
        for r in rows:
            if r.get("status") == "failed":
                cause = r.get("cause") or "unknown"
                acc[cause] = acc.get(cause, 0) + 1
    causes = sorted(set(kb) | set(kc))
    tested = [c for c in causes if max(kb.get(c, 0), kc.get(c, 0)) >= min_count]
    if not tested:
        return CheckResult("causes", "pass", "none", "no cause is frequent enough to compare",
                           {"baseline": kb, "canary": kc, "tested": 0})
    alpha_each = policy.alpha / len(tested)
    rows_out = []
    for c in tested:
        rb, rc = kb.get(c, 0) / nb, kc.get(c, 0) / nc
        delta = rc - rb
        p = fisher_exact_two_sided(kb.get(c, 0), nb, kc.get(c, 0), nc)
        rows_out.append({"cause": c, "baseline_rate": rb, "canary_rate": rc, "delta": delta, "p_value": p,
                         "baseline_count": kb.get(c, 0), "canary_count": kc.get(c, 0),
                         "new": kb.get(c, 0) == 0 and kc.get(c, 0) > 0,
                         "significant": p < alpha_each and abs(delta) >= policy.min_effect})
    worse = sorted((r for r in rows_out if r["significant"] and r["delta"] > 0), key=lambda r: -r["delta"])
    details = {"alpha_each": alpha_each, "causes": rows_out}
    if not worse:
        return CheckResult("causes", "pass", "none", "no failure cause became significantly more common", details)
    top = worse[0]
    names = ", ".join(r["cause"] + (" (new)" if r["new"] else "") for r in worse)
    msg = (f"more common in canary: {names}; largest is {top['cause']} "
           f"{top['baseline_rate']:.1%} -> {top['canary_rate']:.1%}")
    if top["delta"] >= policy.rollback_effect:
        return CheckResult("causes", "fail", "major", msg, details)
    return CheckResult("causes", "warn", "none", msg, details)


# --------------------------------------------------------------------------- replay
def check_replay(replay: Mapping[str, Any] | None, policy: Policy) -> CheckResult:
    """`replay` is the stored summary of the latest replay of the regression suite on the candidate."""
    if replay is None:
        if policy.require_replay:
            return CheckResult("replay", "fail", "minor",
                               "policy requires a regression replay of the candidate and none exists", {})
        return _skipped("replay", "no regression replay found for the candidate version")
    summary = replay.get("summary") or {}
    newly = list(summary.get("newly_failing") or [])
    total = int(summary.get("total") or 0)
    details = {"replay_id": replay.get("id"), "verdict": replay.get("verdict"), "total": total,
               "newly_failing": newly, "still_failing": list(summary.get("still_failing") or []),
               "fixed": list(summary.get("fixed") or [])}
    limit = max(policy.replay_rollback_min, int(round(policy.replay_rollback_share * total)))
    if len(newly) >= limit:
        return CheckResult("replay", "fail", "major",
                           f"{len(newly)} regression case(s) that used to pass now fail", details)
    if newly:
        return CheckResult("replay", "fail", "minor",
                           f"{len(newly)} regression case(s) that used to pass now fail", details)
    if replay.get("verdict") == "incomplete":
        return CheckResult("replay", "warn", "none",
                           "the replay did not verify every case; unverified cases are not evidence of a pass", details)
    return CheckResult("replay", "pass", "none", "no regression case newly fails", details)


# --------------------------------------------------------------------------- change attribution
def check_change(
    diff: Mapping[str, Any], causes_check: CheckResult | None, other_checks: Sequence[CheckResult],
) -> CheckResult:
    """Informational: when the candidate looks worse, say what kind of change best explains it.
    It never blocks a release by itself; the other checks decide. See rootcause.change for the rules."""
    from ..rootcause.change import attribute_change

    regressed = any(c.status in ("warn", "fail") for c in other_checks)
    worse: list[str] = []
    if causes_check is not None and causes_check.status != "skipped":
        worse = [r["cause"] for r in (causes_check.details.get("causes") or []) if r.get("significant") and r.get("delta", 0) > 0]
    found = attribute_change(diff, worse, regressed=regressed)
    details = {"changed": list(diff.get("changed") or []), "diff": dict(diff.get("details") or {}),
               "worse_causes": worse, "attribution": found}
    if not regressed:
        return CheckResult("change", "skipped", "none", "nothing regressed, so there is nothing to attribute", details)
    top = found[0]
    return CheckResult("change", "pass", "none", f"likely {top['cause'].replace('_', ' ')} ({top['confidence']}): {top['why']}", details)
