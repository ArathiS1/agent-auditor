"""Aggregate many diagnoses: what fails most, where, and whether a cause clusters in one version.
Works on plain dicts so the same code serves stored database rows and in-memory benchmarks."""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from ..evals.stats import fisher_exact_two_sided
from .classify import TAXONOMY

UNVERSIONED = "(unversioned)"


def _top(counter: Counter, n: int = 3) -> list[dict[str, Any]]:
    return [{"name": k, "count": v} for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0]))) if k][:n]


def summarize(rows: Sequence[Mapping[str, Any]], *, examples: int = 3) -> dict[str, Any]:
    """rows: dicts with run_id, status ('healthy'|'failed'), cause, strength, subcause, step.
    Pass them newest first so the example runs are the most recent ones."""
    total = len(rows)
    failed = [r for r in rows if r.get("status") == "failed"]
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for r in failed:
        groups.setdefault(r.get("cause") or "unknown", []).append(r)
    causes = []
    for cause, items in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        info = TAXONOMY.get(cause, {})
        causes.append({
            "cause": cause,
            "count": len(items),
            "share_of_failures": round(len(items) / len(failed), 4),
            "share_of_runs": round(len(items) / total, 4) if total else 0.0,
            "strength": dict(Counter(i.get("strength") or "weak" for i in items)),
            "top_steps": _top(Counter(i.get("step") for i in items)),
            "subcauses": _top(Counter(i.get("subcause") for i in items)),
            "example_run_ids": [i["run_id"] for i in items[:examples]],
            "meaning": info.get("meaning", ""),
            "advice": info.get("advice", []),
        })
    return {
        "total_runs": total, "failed_runs": len(failed), "healthy_runs": total - len(failed),
        "failure_rate": round(len(failed) / total, 4) if total else None, "causes": causes,
    }


def version_associations(
    rows: Sequence[Mapping[str, Any]],
    *,
    alpha: float = 0.01,
    min_runs: int = 20,
    min_count: int = 5,
    min_effect: float = 0.10,
) -> dict[str, Any]:
    """Which causes are unusually common (or rare) in one version compared with all the others.

    For each (cause, version) pair with enough runs, compare the version's rate of that cause with
    the rate in the remaining runs (Fisher exact, alpha split across the pairs tested). This finds
    association, not proof: traffic or data can change at the same time as a deploy.
    """
    n_by_version: Counter = Counter()
    k_by: Counter = Counter()
    cause_total: Counter = Counter()
    for r in rows:
        v = r.get("version") or UNVERSIONED
        n_by_version[v] += 1
        if r.get("status") == "failed":
            c = r.get("cause") or "unknown"
            k_by[(c, v)] += 1
            cause_total[c] += 1
    total = sum(n_by_version.values())
    versions = [v for v, n in n_by_version.items() if n >= min_runs and total - n >= min_runs]
    pairs = [(c, v) for c, kc in cause_total.items() if kc >= min_count for v in versions]
    if len(n_by_version) < 2 or not pairs:
        return {"tested": 0, "alpha_each": None, "associations": [],
                "note": "needs at least two versions with enough runs each, and a cause seen often enough"}
    alpha_each = alpha / len(pairs)
    found: list[dict[str, Any]] = []
    for c, v in pairs:
        n_v = n_by_version[v]
        k_v = k_by[(c, v)]
        k_rest = cause_total[c] - k_v
        n_rest = total - n_v
        rate_v, rate_rest = k_v / n_v, k_rest / n_rest
        delta = rate_v - rate_rest
        if abs(delta) < min_effect or max(k_v, k_rest) < min_count:
            continue
        p = fisher_exact_two_sided(k_v, n_v, k_rest, n_rest)
        if p < alpha_each:
            found.append({
                "cause": c, "version": v, "direction": "more_common" if delta > 0 else "less_common",
                "rate_in_version": round(rate_v, 4), "rate_elsewhere": round(rate_rest, 4),
                "count_in_version": k_v, "runs_in_version": n_v, "p_value": p,
            })
    if len(versions) == 2:  # with two versions "more common in A" and "less common in B" are the same finding
        found = [f for f in found if f["direction"] == "more_common"]
    found.sort(key=lambda d: (d["p_value"], d["direction"] != "more_common", d["cause"], d["version"]))
    return {"tested": len(pairs), "alpha_each": alpha_each, "associations": found,
            "note": "association within these runs, not proof of cause; other changes can coincide with a deploy"}
