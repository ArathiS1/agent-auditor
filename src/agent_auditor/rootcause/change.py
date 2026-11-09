"""What changed between two versions, and which kind of change best explains a regression.

The per-run classifier looks at one run and cannot know that a prompt or model changed. This module
looks at two *versions* instead:

1. `diff_versions` compares prompt and config and groups the differences into components:
   prompt, model (model name and sampling settings), retrieval (top_k, thresholds, chunking, embeddings),
   tools, and other.
2. `attribute_change` combines that diff with the causes that got worse and names the most likely
   kind of change: prompt_regression, model_change, retrieval_config_change, tool_config_change,
   config_change, multiple_changes, or source_data_change (the failures look like retrieval problems
   but no retrieval setting changed, so the index or the documents did).

Honest limits: this is attribution from evidence that is available, not proof. When several things
changed at once it says so instead of picking one. Fix one thing at a time to be sure.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

MODEL_KEYS = frozenset({"model", "model_name", "llm", "llm_model", "chat_model", "engine", "provider",
                        "temperature", "top_p", "top_k_sampling", "max_tokens", "max_output_tokens", "seed",
                        "frequency_penalty", "presence_penalty", "stop"})
RETRIEVAL_KEYS = frozenset({"top_k", "k", "min_score", "score_threshold", "similarity_threshold", "threshold",
                            "chunk_size", "chunk_overlap", "chunking", "embedding_model", "embedder", "embeddings",
                            "retriever", "rerank", "reranker", "rerank_top_n", "index", "collection", "search_type",
                            "mmr", "fetch_k", "hybrid", "alpha"})
TOOL_KEYS = frozenset({"tools", "tool_choice", "functions", "tool_timeout", "timeout", "request_timeout",
                       "max_retries", "retries", "max_steps", "max_iterations"})

COMPONENTS = ("prompt", "model", "retrieval", "tools", "other")

# Which failure causes each kind of change can plausibly produce.
_CONSISTENT = {
    "prompt": {"ungrounded_answer", "empty_output", "slow_run", "unknown", "retrieval_low_relevance"},
    "model": {"llm_error", "ungrounded_answer", "empty_output", "slow_run", "unknown"},
    "retrieval": {"retrieval_empty", "retrieval_low_relevance", "retrieval_error", "ungrounded_answer", "slow_run"},
    "tools": {"tool_timeout", "tool_error", "slow_run", "agent_exception"},
    "other": set(),
}
_NAMES = {"prompt": "prompt_regression", "model": "model_change", "retrieval": "retrieval_config_change",
          "tools": "tool_config_change", "other": "config_change"}
_RETRIEVAL_CAUSES = {"retrieval_empty", "retrieval_low_relevance"}


def _component(key: str) -> str:
    k = key.lower()
    if k in MODEL_KEYS:
        return "model"
    if k in RETRIEVAL_KEYS:
        return "retrieval"
    if k in TOOL_KEYS:
        return "tools"
    return "other"


def _same(a: Any, b: Any) -> bool:
    try:
        return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return a == b


def diff_versions(
    baseline_prompt: str | None, baseline_config: Mapping[str, Any] | None,
    candidate_prompt: str | None, candidate_config: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Group the differences between two versions by component."""
    bc, cc = dict(baseline_config or {}), dict(candidate_config or {})
    keys: dict[str, list[dict[str, Any]]] = {c: [] for c in COMPONENTS}
    for key in sorted(set(bc) | set(cc)):
        if key in bc and key in cc and _same(bc[key], cc[key]):
            continue
        keys[_component(key)].append({"key": key, "from": bc.get(key), "to": cc.get(key)})
    prompt_changed = (baseline_prompt or "") != (candidate_prompt or "")
    if prompt_changed:
        keys["prompt"].append({"key": "prompt", "from_chars": len(baseline_prompt or ""), "to_chars": len(candidate_prompt or "")})
    changed = [c for c in COMPONENTS if keys[c]]
    return {"changed": changed, "details": {c: keys[c] for c in changed}}


def attribute_change(diff: Mapping[str, Any], worse_causes: Sequence[str], *, regressed: bool) -> list[dict[str, Any]]:
    """Name the most likely kind of change behind a regression. Returns [] when nothing regressed."""
    if not regressed:
        return []
    changed = list(diff.get("changed") or [])
    worse = set(worse_causes)
    out: list[dict[str, Any]] = []
    if not changed:
        out.append({"cause": "source_data_change", "confidence": "moderate",
                    "why": "the prompt and settings are identical, so something outside them changed: the documents or index, "
                           "an upstream service, or the kind of traffic the candidate received"})
        return out
    if len(changed) == 1:
        comp = changed[0]
        consistent = not worse or bool(worse & _CONSISTENT[comp]) or comp == "other"
        why = f"only the {comp} changed between the two versions"
        if worse:
            why += f"; the causes that got worse ({', '.join(sorted(worse))}) " + (
                "fit that kind of change" if consistent else "do not obviously fit that kind of change, so something else may be involved")
        out.append({"cause": _NAMES[comp], "confidence": "strong" if consistent and worse else "moderate",
                    "why": why, "changed": changed})
    else:
        out.append({"cause": "multiple_changes", "confidence": "weak",
                    "why": f"{' and '.join(changed)} all changed together, so the regression cannot be pinned on one of them; "
                           "revert one at a time and compare", "changed": changed})
    if worse & _RETRIEVAL_CAUSES and "retrieval" not in changed:
        out.append({"cause": "source_data_change", "confidence": "moderate",
                    "why": "retrieval got worse but no retrieval setting changed, so check whether the documents or index changed"})
    return out
