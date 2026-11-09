"""Small statistics helpers in pure Python (no numpy/scipy dependency)."""
from __future__ import annotations

import math


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion. Sensible even for small n and rates near 0 or 1."""
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _log_comb(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def fisher_exact_two_sided(k1: int, n1: int, k2: int, n2: int) -> float:
    """Two-sided Fisher exact test p-value for two pass counts (k1 of n1 vs k2 of n2).

    Exact, so it stays valid for small samples where a z-test would not.
    """
    if min(n1, n2) <= 0:
        return 1.0
    if not (0 <= k1 <= n1 and 0 <= k2 <= n2):
        raise ValueError("pass counts must be between 0 and the sample size")
    total_pass = k1 + k2
    total = n1 + n2
    lo = max(0, total_pass - n2)
    hi = min(total_pass, n1)
    denom = _log_comb(total, n1)

    def log_pmf(k: int) -> float:
        return _log_comb(total_pass, k) + _log_comb(total - total_pass, n1 - k) - denom

    observed = log_pmf(k1)
    cutoff = observed + 1e-7
    p = sum(math.exp(lp) for lp in (log_pmf(k) for k in range(lo, hi + 1)) if lp <= cutoff)
    return min(1.0, p)
