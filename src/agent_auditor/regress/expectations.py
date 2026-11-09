"""Expectations: what a test case requires of a future run, and how a replayed run is graded.

An expectation is a plain dict so it can be stored as JSON and edited by a person:
  {"type": "scorer", "scorer": "retrieval_nonempty", "params": {"min_docs": 1}}
  {"type": "output_contains", "value": "refund"}
  {"type": "output_not_contains", "value": "I cannot help"}
Scorer expectations reuse the Phase 3 scorers, so a test asks "does this scorer now pass?"
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..evals.expect import TEXT_KINDS, check_text_expectation, validate_text_expectation
from ..evals.scorers import _extract_text, evaluate_run
from ..evals.types import RunView, ScorerSpec

PASS, FAIL, UNVERIFIED, MISSING = "pass", "fail", "unverified", "missing"
WEAK_SCORERS = frozenset({"grounded_overlap"})  # heuristic; too noisy to gate on by default
MAX_CHECKS = 20
_JSON_SCALARS = (str, int, float, bool)


def derive_expectations(
    failed: Sequence[Mapping[str, Any]], specs: Mapping[str, ScorerSpec], *, include_weak: bool = False
) -> list[dict[str, Any]]:
    """One expectation per failed scorer: 'this scorer must pass now', using the same parameters
    (threshold, budget, ...) that the original failure was judged with."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in failed:
        name = str(item["scorer"])
        if name in seen or (name in WEAK_SCORERS and not include_weak):
            continue
        seen.add(name)
        spec = specs.get(name)
        details = item.get("details") or {}
        defaults = spec.defaults if spec else {}
        params = {k: details[k] for k in defaults if k in details and isinstance(details[k], _JSON_SCALARS)}
        out.append({"type": "scorer", "scorer": name, "params": params, "why": str(item.get("reason") or "")[:300]})
    return out


def validate_expectations(expectations: Any, known_scorers: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Check a user-supplied list of expectations and return a cleaned copy. Raises ValueError."""
    if not isinstance(expectations, list) or not expectations:
        raise ValueError("expectations must be a non-empty list")
    if len(expectations) > MAX_CHECKS:
        raise ValueError(f"at most {MAX_CHECKS} expectations per test case")
    cleaned: list[dict[str, Any]] = []
    for i, e in enumerate(expectations):
        if not isinstance(e, dict):
            raise ValueError(f"expectation {i} must be an object")
        kind = e.get("type")
        if kind == "scorer":
            name = e.get("scorer")
            if not isinstance(name, str) or not name:
                raise ValueError(f"expectation {i}: 'scorer' must be a scorer name")
            if known_scorers is not None and name not in known_scorers:
                raise ValueError(f"expectation {i}: unknown scorer '{name}'")
            params = e.get("params") or {}
            if not isinstance(params, dict) or not all(isinstance(v, _JSON_SCALARS) for v in params.values()):
                raise ValueError(f"expectation {i}: 'params' must be an object of simple values")
            cleaned.append({"type": "scorer", "scorer": name, "params": params, "why": str(e.get("why") or "")[:300]})
        elif kind in TEXT_KINDS:
            cleaned.append(validate_text_expectation(i, e))
        else:
            raise ValueError(f"expectation {i}: unknown type {kind!r}")
    return cleaned


@dataclass(frozen=True)
class CheckOutcome:
    check: str
    status: str  # pass | fail | not_applicable | error
    reason: str
    score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "status": self.status, "reason": self.reason, "score": self.score}


@dataclass(frozen=True)
class CaseGrade:
    outcome: str  # pass | fail | unverified
    checks: tuple[CheckOutcome, ...] = field(default_factory=tuple)


def _output_text(run: RunView) -> str:
    pieces: list[str] = []
    _extract_text(run.output, pieces, [50_000])
    return " ".join(pieces).lower()


def grade_case(expectations: Sequence[Mapping[str, Any]], run: RunView) -> CaseGrade:
    """Grade one replayed run. Any failed check fails the case. If nothing failed but some check
    could not be evaluated (scorer not applicable to the new run, or the scorer itself errored),
    the case is 'unverified', never silently 'pass'."""
    checks: list[CheckOutcome] = []
    for e in expectations:
        kind = e.get("type")
        if kind == "scorer":
            name = str(e.get("scorer"))
            results, errors = evaluate_run(run, [name], {name: dict(e.get("params") or {})})
            if name in errors:
                checks.append(CheckOutcome(name, "error", errors[name]))
            elif results.get(name) is None:
                checks.append(CheckOutcome(name, "not_applicable", "scorer does not apply to the replayed run"))
            else:
                r = results[name]
                checks.append(CheckOutcome(name, "pass" if r.passed else "fail", r.reason, r.score))
        elif kind in TEXT_KINDS:
            ok, reason = check_text_expectation(e, _output_text(run))
            label = e.get("value") or e.get("pattern") or ",".join(map(str, e.get("values", [])))
            checks.append(CheckOutcome(f"{kind}:{label}"[:80], "pass" if ok else "fail", reason))
        else:
            checks.append(CheckOutcome(str(kind), "error", f"unknown expectation type {kind!r}"))
    if any(c.status == "fail" for c in checks):
        outcome = FAIL
    elif checks and all(c.status == "pass" for c in checks):
        outcome = PASS
    else:
        outcome = UNVERIFIED
    return CaseGrade(outcome, tuple(checks))


def summarize_replay(outcomes: Mapping[str, str], previous: Mapping[str, str | None]) -> dict[str, Any]:
    """Aggregate per-case outcomes into a verdict.

    previous maps case id -> outcome in the last earlier replay (None if never replayed, meaning
    the case still stands as the original production failure).
    - fixed:          passes now, did not pass last time (or never replayed)
    - newly_failing:  fails now, passed last time  <- a regression
    - still_failing:  fails now, did not pass last time
    Verdict: fail if any case fails; pass if all pass; otherwise incomplete (some unverified/missing).
    """
    counts = {PASS: 0, FAIL: 0, UNVERIFIED: 0, MISSING: 0}
    fixed: list[str] = []
    newly: list[str] = []
    still: list[str] = []
    for cid, outcome in outcomes.items():
        counts[outcome] = counts.get(outcome, 0) + 1
        prev = previous.get(cid)
        if outcome == PASS and prev != PASS:
            fixed.append(cid)
        elif outcome == FAIL and prev == PASS:
            newly.append(cid)
        elif outcome == FAIL:
            still.append(cid)
    total = len(outcomes)
    if counts[FAIL]:
        verdict = "fail"
    elif total and counts[PASS] == total:
        verdict = "pass"
    else:
        verdict = "incomplete"
    return {
        "verdict": verdict, "total": total, "passed": counts[PASS], "failed": counts[FAIL],
        "unverified": counts[UNVERIFIED], "missing": counts[MISSING],
        "pass_rate": round(counts[PASS] / total, 4) if total else None,
        "fixed": sorted(fixed), "newly_failing": sorted(newly), "still_failing": sorted(still),
    }
