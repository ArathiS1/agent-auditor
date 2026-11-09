"""Turn the check results into one verdict: promote, hold, rollback or continue."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from .checks import CheckResult, Policy

VERDICTS = ("promote", "hold", "rollback", "continue")


@dataclass(frozen=True)
class Decision:
    verdict: str  # promote | hold | rollback | continue
    reasons: list[str]
    blocking: list[str] = field(default_factory=list)  # names of the checks behind the verdict
    warnings: list[str] = field(default_factory=list)


def decide(checks: Sequence[CheckResult], *, canary_runs: int, baseline_runs: int, policy: Policy) -> Decision:
    """Rules, in order:
      1. any major failure                        -> rollback
      2. any minor failure                        -> hold
      3. not enough live runs on either side      -> continue (keep collecting; nothing is proven)
      4. the eval check could not be run          -> continue (promoting on no evidence would be a guess)
      5. warnings and policy.warnings_block       -> hold
      6. otherwise                                -> promote
    Replay failures count even when live traffic is thin, because replay does not depend on it.
    """
    fails = [c for c in checks if c.status == "fail"]
    warns = [c for c in checks if c.status == "warn"]
    warnings = [f"{c.name}: {c.reason}" for c in warns]

    major = [c for c in fails if c.severity == "major"]
    if major:
        return Decision("rollback", [f"{c.name}: {c.reason}" for c in major], [c.name for c in major], warnings)
    if fails:
        return Decision("hold", [f"{c.name}: {c.reason}" for c in fails], [c.name for c in fails], warnings)

    reasons: list[str] = []
    if canary_runs < policy.min_canary_runs:
        reasons.append(f"only {canary_runs} canary runs so far; need {policy.min_canary_runs}")
    if baseline_runs < policy.min_baseline_runs:
        reasons.append(f"only {baseline_runs} baseline runs in the window; need {policy.min_baseline_runs}")
    if reasons:
        return Decision("continue", reasons, [], warnings)

    evals = next((c for c in checks if c.name == "evals"), None)
    if evals is None or evals.status == "skipped":
        return Decision("continue", ["no eval comparison is available yet; evaluate both sides before deciding"],
                        ["evals"], warnings)
    if warns and policy.warnings_block:
        return Decision("hold", warnings, [c.name for c in warns], warnings)

    ok = [c.name for c in checks if c.status == "pass"]
    skipped = [c.name for c in checks if c.status == "skipped"]
    why = ["no check failed: " + ", ".join(ok)]
    if skipped:
        why.append("not evaluated: " + ", ".join(skipped))
    return Decision("promote", why, [], warnings)
