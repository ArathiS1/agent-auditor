"""Compare two agent versions on pass rates, scorer by scorer.

Decision rule (per scorer)
- insufficient_data : either side has fewer than `min_samples` applicable runs
- regression        : pass rate dropped by at least `min_effect` AND Fisher exact p < alpha
- improvement       : pass rate rose by at least `min_effect` AND p < alpha
- inconclusive      : the change is at least `min_effect` but not statistically significant
- no_change         : otherwise

Because several scorers are tested at once, alpha is split across the testable scorers
(Bonferroni) unless `correct_multiple=False`, to avoid flagging regressions by chance.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .stats import fisher_exact_two_sided, wilson_interval


@dataclass(frozen=True)
class Counts:
    n: int  # applicable runs
    passed: int
    mean_score: float | None = None


@dataclass(frozen=True)
class ScorerComparison:
    scorer: str
    verdict: str
    reason: str
    baseline_n: int
    candidate_n: int
    baseline_pass_rate: float | None
    candidate_pass_rate: float | None
    delta: float | None
    p_value: float | None
    alpha_used: float
    baseline_ci: tuple[float, float] | None
    candidate_ci: tuple[float, float] | None
    baseline_mean_score: float | None = None
    candidate_mean_score: float | None = None


@dataclass(frozen=True)
class Comparison:
    overall_verdict: str
    scorers: list[ScorerComparison]
    alpha: float
    min_samples: int
    min_effect: float
    notes: list[str] = field(default_factory=list)


def compare_counts(
    baseline: dict[str, Counts],
    candidate: dict[str, Counts],
    *,
    min_samples: int = 20,
    min_effect: float = 0.05,
    alpha: float = 0.05,
    correct_multiple: bool = True,
) -> Comparison:
    if min_samples < 1:
        raise ValueError("min_samples must be at least 1")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")
    if not 0 <= min_effect <= 1:
        raise ValueError("min_effect must be between 0 and 1")

    names = sorted(set(baseline) | set(candidate))
    empty = Counts(0, 0)

    def testable(name: str) -> bool:
        return baseline.get(name, empty).n >= min_samples and candidate.get(name, empty).n >= min_samples

    testable_count = sum(1 for n in names if testable(n))
    alpha_each = alpha / testable_count if (correct_multiple and testable_count > 1) else alpha

    rows: list[ScorerComparison] = []
    for name in names:
        b, c = baseline.get(name, empty), candidate.get(name, empty)
        if not testable(name):
            rows.append(ScorerComparison(
                name, "insufficient_data",
                f"need at least {min_samples} runs on each side (baseline {b.n}, candidate {c.n})",
                b.n, c.n,
                b.passed / b.n if b.n else None, c.passed / c.n if c.n else None,
                None, None, alpha_each, None, None, b.mean_score, c.mean_score,
            ))
            continue
        br, cr = b.passed / b.n, c.passed / c.n
        delta = cr - br
        p = fisher_exact_two_sided(b.passed, b.n, c.passed, c.n)
        if delta <= -min_effect and p < alpha_each:
            verdict, why = "regression", f"pass rate fell {abs(delta):.1%} (p={p:.3g})"
        elif delta >= min_effect and p < alpha_each:
            verdict, why = "improvement", f"pass rate rose {delta:.1%} (p={p:.3g})"
        elif abs(delta) >= min_effect:
            verdict, why = "inconclusive", f"pass rate changed {delta:+.1%} but p={p:.3g} is not below {alpha_each:.3g}; collect more runs"
        else:
            verdict, why = "no_change", f"pass rate changed {delta:+.1%} (p={p:.3g})"
        rows.append(ScorerComparison(
            name, verdict, why, b.n, c.n, br, cr, delta, p, alpha_each,
            wilson_interval(b.passed, b.n), wilson_interval(c.passed, c.n), b.mean_score, c.mean_score,
        ))

    verdicts = {r.verdict for r in rows}
    notes: list[str] = []
    if not rows or verdicts == {"insufficient_data"}:
        overall = "insufficient_data"
        notes.append("no scorer has enough runs on both sides to compare")
    elif "regression" in verdicts:
        overall = "regression"
    elif "inconclusive" in verdicts:
        overall = "inconclusive"
    elif "improvement" in verdicts:
        overall = "improvement"
    else:
        overall = "no_change"
    skipped = [r.scorer for r in rows if r.verdict == "insufficient_data"]
    if skipped and overall != "insufficient_data":
        notes.append("not compared (too few runs): " + ", ".join(skipped))
    if correct_multiple and testable_count > 1:
        notes.append(f"alpha {alpha} split across {testable_count} scorers (each tested at {alpha_each:.4f})")
    return Comparison(overall, rows, alpha, min_samples, min_effect, notes)
