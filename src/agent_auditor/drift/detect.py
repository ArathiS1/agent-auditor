"""Compare a recent window of runs with a reference window and report what shifted.

Signals
- numeric (KS test; severity from the KS statistic, PSI reported for reference): latency, input/output length, step count, retrieval score,
  number of retrieved documents
- rate (Fisher exact test; severity from the size of the change): run errors, step errors, empty
  retrieval, plus any extra pass rates supplied by the caller (for example eval pass rates)
- term mix (permutation test on Jensen-Shannon divergence): words in inputs, which tools are called

Significance is judged at alpha / (number of testable signals), so a long list of signals does not
produce false alarms on its own. Severity depends on the SIZE of the change, not only on the
p-value, because with enough runs even a trivial shift is "significant".
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..evals.stats import fisher_exact_two_sided, wilson_interval
from ..evals.types import RunView
from .signals import BAG_SIGNALS, NUMERIC_SIGNALS, RATE_SIGNALS, RunSignals, extract_signals
from .stats import ks_two_sample, permutation_js_test, psi, summarize

SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
MAX_BAG_DOCS = 300


@dataclass(frozen=True)
class Thresholds:
    """Effect-size cut-offs. These are sensible defaults, not laws; tune them to your agent."""

    ks_medium: float = 0.30  # KS statistic: share of runs by which the two distributions differ
    ks_high: float = 0.50
    rate_low: float = 0.05
    rate_medium: float = 0.10
    rate_high: float = 0.20
    js_medium: float = 0.08
    js_high: float = 0.20


@dataclass(frozen=True)
class SignalDrift:
    name: str
    kind: str  # numeric | rate | terms
    severity: str  # none | low | medium | high | insufficient_data
    significant: bool
    p_value: float | None
    alpha_used: float
    effect_name: str
    effect: float | None
    reference_n: int
    current_n: int
    reference: dict[str, Any]
    current: dict[str, Any]
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DriftReport:
    overall: str  # stable | minor_drift | drift | insufficient_data
    signals: list[SignalDrift]
    reference_runs: int
    current_runs: int
    alpha: float
    min_samples: int
    notes: list[str] = field(default_factory=list)


def _band(value: float, medium: float, high: float) -> str:
    return "high" if value >= high else "medium" if value >= medium else "low"


def _pct(old: float, new: float) -> str:
    if old == 0:
        return "n/a" if new == 0 else "from 0"
    return f"{(new - old) / abs(old):+.0%}"


def _term_shifts(ref_docs: Sequence[Sequence[str]], cur_docs: Sequence[Sequence[str]], top: int = 5) -> dict[str, list[dict[str, Any]]]:
    def share(docs: Sequence[Sequence[str]]) -> dict[str, float]:
        counts: dict[str, int] = {}
        for d in docs:
            for t in set(d):
                counts[t] = counts.get(t, 0) + 1
        return {t: c / len(docs) for t, c in counts.items()}

    r, c = share(ref_docs), share(cur_docs)
    diffs = sorted(((c.get(t, 0.0) - r.get(t, 0.0), t) for t in set(r) | set(c)), key=lambda x: (-x[0], x[1]))

    def row(diff: float, term: str) -> dict[str, Any]:
        return {"term": term, "reference_share": round(r.get(term, 0.0), 4), "current_share": round(c.get(term, 0.0), 4)}

    up = [row(d, t) for d, t in diffs[:top] if d > 0]
    down = [row(d, t) for d, t in sorted(diffs, key=lambda x: (x[0], x[1]))[:top] if d < 0]
    return {"more_common": up, "less_common": down}


def detect_drift(
    reference_runs: Sequence[RunView],
    current_runs: Sequence[RunView],
    *,
    extra_rates: dict[str, tuple[tuple[int, int], tuple[int, int]]] | None = None,
    min_samples: int = 30,
    alpha: float = 0.01,
    correct_multiple: bool = True,
    thresholds: Thresholds = Thresholds(),
    seed: int = 0,
) -> DriftReport:
    """`extra_rates` maps a name to ((reference_passed, reference_n), (current_passed, current_n))."""
    if min_samples < 2:
        raise ValueError("min_samples must be at least 2")
    if not 0 < alpha < 1:
        raise ValueError("alpha must be between 0 and 1")

    n_ref, n_cur = len(reference_runs), len(current_runs)
    if n_ref < min_samples or n_cur < min_samples:
        return DriftReport(
            "insufficient_data", [], n_ref, n_cur, alpha, min_samples,
            [f"need at least {min_samples} runs in each window (reference {n_ref}, current {n_cur})"],
        )

    ref: list[RunSignals] = [extract_signals(r) for r in reference_runs]
    cur: list[RunSignals] = [extract_signals(r) for r in current_runs]

    numeric = {n: ([s.numeric[n] for s in ref if n in s.numeric], [s.numeric[n] for s in cur if n in s.numeric]) for n in NUMERIC_SIGNALS}
    rates: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {}
    for n in RATE_SIGNALS:
        rv = [s.flags[n] for s in ref if n in s.flags]
        cv = [s.flags[n] for s in cur if n in s.flags]
        rates[n] = ((sum(rv), len(rv)), (sum(cv), len(cv)))
    rates.update(extra_rates or {})
    bags = {n: ([sorted(s.bags[n]) for s in ref if n in s.bags], [sorted(s.bags[n]) for s in cur if n in s.bags]) for n in BAG_SIGNALS}

    testable = (
        [n for n, (a, b) in numeric.items() if len(a) >= min_samples and len(b) >= min_samples]
        + [n for n, ((_, na), (_, nb)) in rates.items() if na >= min_samples and nb >= min_samples]
        + [n for n, (a, b) in bags.items() if len(a) >= min_samples and len(b) >= min_samples]
    )
    m = len(testable)
    alpha_each = alpha / m if (correct_multiple and m > 1) else alpha
    notes: list[str] = []
    if correct_multiple and m > 1:
        notes.append(f"alpha {alpha} split across {m} signals (each tested at {alpha_each:.5f})")

    rows: list[SignalDrift] = []

    def insufficient(name: str, kind: str, a: int, b: int, ref_summary: dict, cur_summary: dict) -> None:
        rows.append(SignalDrift(
            name, kind, "insufficient_data", False, None, alpha_each, "", None, a, b, ref_summary, cur_summary,
            f"need at least {min_samples} observations in each window (reference {a}, current {b})",
        ))

    for name, (a, b) in numeric.items():
        if name not in testable:
            if a or b:
                insufficient(name, "numeric", len(a), len(b), summarize(a), summarize(b))
            continue
        d, p = ks_two_sample(a, b)
        shift = psi(a, b)
        sig = p < alpha_each
        severity = "none" if not sig else _band(d, thresholds.ks_medium, thresholds.ks_high)
        sa, sb = summarize(a), summarize(b)
        reason = (f"median {sa['median']:.4g} -> {sb['median']:.4g} ({_pct(sa['median'], sb['median'])}), "
                  f"KS D={d:.2f}, p={p:.3g}")
        rows.append(SignalDrift(name, "numeric", severity, sig, p, alpha_each, "ks_statistic", d, len(a), len(b), sa, sb, reason,
                                {"psi": shift}))

    for name, ((ka, na), (kb, nb)) in rates.items():
        ref_summary = {"n": na, "rate": (ka / na) if na else None}
        cur_summary = {"n": nb, "rate": (kb / nb) if nb else None}
        if name not in testable:
            if na or nb:
                insufficient(name, "rate", na, nb, ref_summary, cur_summary)
            continue
        ra, rb = ka / na, kb / nb
        delta = rb - ra
        p = fisher_exact_two_sided(ka, na, kb, nb)
        sig = p < alpha_each
        size = abs(delta)
        severity = "none" if (not sig or size < thresholds.rate_low) else _band(size, thresholds.rate_medium, thresholds.rate_high)
        reason = f"rate {ra:.1%} -> {rb:.1%} ({delta:+.1%}), p={p:.3g}"
        rows.append(SignalDrift(name, "rate", severity, sig, p, alpha_each, "rate_change", delta, na, nb, ref_summary, cur_summary, reason,
                                {"reference_ci": list(wilson_interval(ka, na)), "current_ci": list(wilson_interval(kb, nb))}))

    for name, (a, b) in bags.items():
        ref_summary = {"n": len(a), "distinct_terms": len({t for d in a for t in d})}
        cur_summary = {"n": len(b), "distinct_terms": len({t for d in b for t in d})}
        if name not in testable:
            if a or b:
                insufficient(name, "terms", len(a), len(b), ref_summary, cur_summary)
            continue
        rng = random.Random(f"{seed}:{name}")
        sample_a = rng.sample(a, MAX_BAG_DOCS) if len(a) > MAX_BAG_DOCS else a
        sample_b = rng.sample(b, MAX_BAG_DOCS) if len(b) > MAX_BAG_DOCS else b
        n_perm = min(2000, max(199, math.ceil(5 / alpha_each)))
        js, p = permutation_js_test(sample_a, sample_b, n_perm, rng)
        floor_p = 1 / (n_perm + 1)
        sig = p < alpha_each or (alpha_each < floor_p and p <= floor_p + 1e-12)
        severity = "none" if not sig else _band(js, thresholds.js_medium, thresholds.js_high)
        shifts = _term_shifts(sample_a, sample_b)
        top = ", ".join(r["term"] for r in shifts["more_common"][:3]) or "none"
        reason = f"term mix distance JS={js:.2f}, p={p:.3g}; more common now: {top}"
        rows.append(SignalDrift(name, "terms", severity, sig, p, alpha_each, "js_divergence", js, len(a), len(b), ref_summary, cur_summary,
                                reason, {**shifts, "permutations": n_perm, "sampled_runs": [len(sample_a), len(sample_b)]}))

    rows.sort(key=lambda r: (-SEVERITY_RANK.get(r.severity, -1), r.name))
    ranks = [SEVERITY_RANK[r.severity] for r in rows if r.severity in SEVERITY_RANK]
    if not ranks:
        overall = "insufficient_data"
        notes.append("no signal has enough observations in both windows")
    elif max(ranks) >= SEVERITY_RANK["medium"]:
        overall = "drift"
    elif max(ranks) == SEVERITY_RANK["low"]:
        overall = "minor_drift"
    else:
        overall = "stable"
    skipped = [r.name for r in rows if r.severity == "insufficient_data"]
    if skipped and overall != "insufficient_data":
        notes.append("not compared (too few observations): " + ", ".join(skipped))
    return DriftReport(overall, rows, n_ref, n_cur, alpha, min_samples, notes)
