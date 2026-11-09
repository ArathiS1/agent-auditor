"""Distribution-shift statistics in pure Python (no numpy/scipy dependency)."""
from __future__ import annotations

import bisect
import math
import random
from collections import Counter
from itertools import chain
from typing import Any, Mapping, Sequence


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile of an already sorted sequence."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("quantile of an empty sequence")
    if n == 1:
        return float(sorted_values[0])
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac)


def summarize(values: Sequence[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    s = sorted(values)
    return {
        "n": len(s), "mean": sum(s) / len(s), "median": quantile(s, 0.5), "p95": quantile(s, 0.95),
        "min": float(s[0]), "max": float(s[-1]),
    }


def ks_two_sample(a: Sequence[float], b: Sequence[float]) -> tuple[float, float]:
    """Two-sample Kolmogorov-Smirnov test. Returns (D, p-value).

    D is the largest gap between the two empirical distributions (0 = identical, 1 = disjoint).
    The p-value uses the standard asymptotic formula with the Stephens small-sample correction;
    it is accurate for roughly 30+ observations per side, which is why drift checks need a
    minimum sample size.
    """
    if not a or not b:
        raise ValueError("both samples must be non-empty")
    xs, ys = sorted(a), sorted(b)
    n, m = len(xs), len(ys)
    d = 0.0
    for v in sorted(set(xs) | set(ys)):
        d = max(d, abs(bisect.bisect_right(xs, v) / n - bisect.bisect_right(ys, v) / m))
    return d, _ks_pvalue(d, n * m / (n + m))


def _ks_pvalue(d: float, effective_n: float) -> float:
    if d <= 0:
        return 1.0
    root = math.sqrt(effective_n)
    lam = (root + 0.12 + 0.11 / root) * d
    total, sign = 0.0, 1.0
    for j in range(1, 101):
        term = sign * 2.0 * math.exp(-2.0 * j * j * lam * lam)
        total += term
        if abs(term) < 1e-12:
            break
        sign = -sign
    return min(1.0, max(0.0, total))


def psi(reference: Sequence[float], current: Sequence[float], bins: int = 10) -> float:
    """Population Stability Index (informational), smoothed for small samples.

    Bins come from the reference: deciles for continuous data, fewer when a window is small (about
    10 observations per bin), and one bin per value for discrete or constant data, plus bins below
    and above the reference range, so a move to never-seen values registers as a large shift.
    Counts get +0.5 smoothing so an empty bin cannot blow the score up.

    The usual cut-offs (0.10 moderate, 0.25 major) come from credit-risk work with thousands of
    rows; on windows of a few dozen runs, treat PSI as a rough guide. Drift severity in this
    package is graded with the KS statistic instead.
    """
    if not reference or not current:
        raise ValueError("both samples must be non-empty")
    bins = max(2, min(bins, len(reference) // 10, len(current) // 10))
    ref_sorted = sorted(reference)
    distinct = sorted(set(ref_sorted))
    if len(distinct) < bins:
        edges = list(distinct) + [math.nextafter(distinct[-1], math.inf)]
    else:
        edges = sorted({quantile(ref_sorted, i / bins) for i in range(1, bins)})

    def smoothed(values: Sequence[float]) -> list[float]:
        counts = [0.0] * (len(edges) + 1)
        for v in values:
            counts[bisect.bisect_right(edges, v)] += 1
        total = len(values) + 0.5 * len(counts)
        return [(c + 0.5) / total for c in counts]

    r, c = smoothed(reference), smoothed(current)
    return sum((ci - ri) * math.log(ci / ri) for ri, ci in zip(r, c))


def js_divergence(a: Mapping[Any, int], b: Mapping[Any, int]) -> float:
    """Jensen-Shannon divergence in bits (0 = identical distributions, 1 = no overlap)."""
    ta, tb = sum(a.values()), sum(b.values())
    if ta == 0 or tb == 0:
        return 0.0 if ta == tb else 1.0
    js = 0.0
    for key in set(a) | set(b):
        p, q = a.get(key, 0) / ta, b.get(key, 0) / tb
        mid = (p + q) / 2
        if p > 0:
            js += 0.5 * p * math.log2(p / mid)
        if q > 0:
            js += 0.5 * q * math.log2(q / mid)
    return max(0.0, min(1.0, js))


def permutation_js_test(
    reference_docs: Sequence[Sequence[str]],
    current_docs: Sequence[Sequence[str]],
    n_perm: int,
    rng: random.Random,
) -> tuple[float, float]:
    """Is the term mix of the current documents different from the reference's?

    Each "document" is the set of terms from one run. The observed Jensen-Shannon divergence is
    compared with the divergence obtained when runs are randomly reassigned to the two windows,
    so the p-value respects that terms within a run are correlated (a plain chi-square would not).
    Returns (js, p-value); the smallest possible p-value is 1 / (n_perm + 1).
    """
    docs = [list(d) for d in reference_docs] + [list(d) for d in current_docs]
    n_ref = len(reference_docs)
    observed = js_divergence(Counter(chain.from_iterable(docs[:n_ref])), Counter(chain.from_iterable(docs[n_ref:])))
    if not docs or n_ref == 0 or n_ref == len(docs):
        return observed, 1.0
    idx = list(range(len(docs)))
    at_least_as_extreme = 0
    for _ in range(n_perm):
        rng.shuffle(idx)
        a = Counter(chain.from_iterable(docs[i] for i in idx[:n_ref]))
        b = Counter(chain.from_iterable(docs[i] for i in idx[n_ref:]))
        if js_divergence(a, b) >= observed - 1e-12:
            at_least_as_extreme += 1
    return observed, (1 + at_least_as_extreme) / (1 + n_perm)
