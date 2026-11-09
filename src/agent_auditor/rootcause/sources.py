"""Find source documents that show up in failing runs far more often than in healthy ones.

"Bad source data" cannot be read from one run: a stale or wrong document looks like a normal retrieval hit.
What can be measured is concentration: if one document appears in many failed runs and rarely in healthy
ones, it deserves a human look. That is a lead, not a verdict.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Sequence

from ..evals.stats import fisher_exact_two_sided
from ..evals.types import RunView

_ID_KEYS = ("source", "doc_id", "document_id", "file", "filename", "path", "url", "title", "id")


def _name(doc: Any) -> str | None:
    if isinstance(doc, str):
        return None  # plain text has no source name
    if not isinstance(doc, dict):
        return None
    for holder in (doc, doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}):
        for key in _ID_KEYS:
            val = holder.get(key)
            if isinstance(val, (str, int)) and str(val).strip():
                return str(val).strip()[:200]
    return None


def run_sources(run: RunView) -> set[str]:
    """Names of the documents the run's retrieval steps returned (empty if the steps do not name them)."""
    found: set[str] = set()
    for step in run.steps:
        if step.kind != "retrieval" or step.status == "error":
            continue
        out = step.output
        if isinstance(out, dict):
            out = out.get("documents") or out.get("docs") or out.get("results") or out.get("hits")
        if isinstance(out, (list, tuple)):
            for doc in out:
                name = _name(doc)
                if name:
                    found.add(name)
    return found


def suspect_sources(
    failed: Iterable[RunView], healthy: Iterable[RunView], *,
    min_count: int = 3, min_failed_share: float = 0.2, min_effect: float = 0.15, alpha: float = 0.01, top: int = 5,
) -> list[dict[str, Any]]:
    f_sets = [run_sources(r) for r in failed]
    h_sets = [run_sources(r) for r in healthy]
    f_sets = [s for s in f_sets if s]
    h_sets = [s for s in h_sets if s]
    nf, nh = len(f_sets), len(h_sets)
    if nf < min_count or nh < min_count:
        return []
    fc: Counter[str] = Counter(n for s in f_sets for n in s)
    hc: Counter[str] = Counter(n for s in h_sets for n in s)
    candidates = [n for n, c in fc.items() if c >= min_count and c / nf >= min_failed_share]
    if not candidates:
        return []
    alpha_each = alpha / len(candidates)
    out: list[dict[str, Any]] = []
    for name in candidates:
        rf, rh = fc[name] / nf, hc.get(name, 0) / nh
        if rf - rh < min_effect:
            continue
        p = fisher_exact_two_sided(fc[name], nf, hc.get(name, 0), nh)
        if p < alpha_each:
            out.append({"source": name, "failed_runs": fc[name], "failed_share": round(rf, 4),
                        "healthy_share": round(rh, 4), "p_value": p,
                        "meaning": "appears in far more failed runs than healthy ones; check whether it is outdated, wrong or badly split"})
    out.sort(key=lambda d: (d["p_value"], -d["failed_share"], d["source"]))
    return out[:top]


def sources_cover(runs: Sequence[RunView]) -> int:
    """How many of these runs name their documents at all (0 means this analysis cannot apply)."""
    return sum(1 for r in runs if run_sources(r))
