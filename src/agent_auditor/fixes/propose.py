"""Turn a failure report into concrete, testable fix candidates. Pure functions: no database, no model call.

Each candidate changes ONE thing (one setting, or one added prompt rule) so a replay result can be
credited to that change. A candidate is either
  - verifiable: a new prompt and/or config that can be registered as a version and replayed, or
  - manual: advice that needs a human (a code bug, a bad source document), shown but never "verified".

The rules are deliberately simple and conservative. They only touch settings that already exist in the
version's config (so the agent can actually read them), and they only propose what the measured causes
point at. An optional AI-written prompt candidate can be added by the API layer.
"""
from __future__ import annotations

import copy
import hashlib
from typing import Any, Mapping, Sequence

GROUNDING_RULE = (
    "Answer only from the provided context. If the context does not contain the answer, say that you could not find it "
    "instead of guessing. Do not add facts that are not in the context."
)
COMPLETE_RULE = "Always end with a final answer written as plain text. If you cannot answer, say so in one sentence."

_TOP_K = ("top_k", "k")
_MIN_SCORE = ("min_score", "score_threshold", "similarity_threshold")
_TIMEOUT = ("timeout", "tool_timeout", "request_timeout")
_RETRIES = ("max_retries", "retries")


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _first(config: Mapping[str, Any], names: Sequence[str]) -> str | None:
    return next((n for n in names if n in config and _num(config[n])), None)


def candidate_id(kind: str, changes: Sequence[Mapping[str, Any]], prompt_add: str | None) -> str:
    raw = repr((kind, [(c["field"], c.get("to")) for c in changes], prompt_add))
    return hashlib.sha256(raw.encode()).hexdigest()[:8]


def _cfg_candidate(cause: str, title: str, rationale: str, base_config: Mapping[str, Any], field: str, new: Any) -> dict[str, Any]:
    cfg = copy.deepcopy(dict(base_config))
    changes = [{"field": field, "from": base_config.get(field), "to": new}]
    cfg[field] = new
    return {"id": candidate_id("config", changes, None), "kind": "config", "verifiable": True, "cause": cause, "source": "rules",
            "title": title, "rationale": rationale, "changes": changes, "prompt": None, "config": cfg}


def _prompt_candidate(cause: str, title: str, rationale: str, base_prompt: str | None, rule: str) -> dict[str, Any] | None:
    if rule.lower() in (base_prompt or "").lower():
        return None  # the rule is already in the prompt
    new = ((base_prompt or "").rstrip() + "\n\n" + rule).strip()
    changes = [{"field": "prompt", "from": None, "to": f"adds: {rule}"}]
    return {"id": candidate_id("prompt", changes, rule), "kind": "prompt", "verifiable": True, "cause": cause, "source": "rules",
            "title": title, "rationale": rationale, "changes": changes, "prompt": new, "config": None}


def _manual(cause: str, title: str, rationale: str, advice: Sequence[str]) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    return {"id": candidate_id("manual", changes, title), "kind": "manual", "verifiable": False, "cause": cause, "source": "rules",
            "title": title, "rationale": rationale, "changes": changes, "prompt": None, "config": None, "advice": list(advice)}


def propose(
    prompt: str | None, config: Mapping[str, Any] | None, report: Mapping[str, Any], *, max_candidates: int = 6,
) -> list[dict[str, Any]]:
    """Fix candidates for the causes in `report` (a root-cause report), most common cause first."""
    cfg = dict(config or {})
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(c: dict[str, Any] | None) -> None:
        if c and c["id"] not in seen:
            seen.add(c["id"])
            out.append(c)

    for item in report.get("causes", []):
        cause = item["cause"]
        share = f"{item.get('count', 0)} failed run(s), {item.get('share_of_failures', 0):.0%} of failures"
        if cause in ("retrieval_empty", "retrieval_low_relevance"):
            k = _first(cfg, _TOP_K)
            if k:
                add(_cfg_candidate(cause, f"Retrieve more passages ({k}: {cfg[k]} -> {min(int(cfg[k]) + 2, 20)})",
                                   f"{share} came from weak or empty retrieval; more candidates can surface the right passage.",
                                   cfg, k, min(int(cfg[k]) + 2, 20)))
            m = _first(cfg, _MIN_SCORE)
            if m:
                lowered = round(max(float(cfg[m]) - 0.1, 0.0), 3)
                add(_cfg_candidate(cause, f"Accept weaker matches ({m}: {cfg[m]} -> {lowered})",
                                   f"{share}: a strict cut-off may be discarding passages that were good enough. "
                                   "Lowering it can also let in wrong ones, which is why it is replayed before being suggested.",
                                   cfg, m, lowered))
            if not k and not m:
                add(_manual(cause, "Make retrieval settings testable",
                            f"{share} came from retrieval, but the version config has no top_k or score threshold to change.",
                            ["Put top_k and the minimum score in the version config and read them in the agent, so fixes can be replayed.",
                             "Check whether the documents that should answer these questions are in the index at all."]))
        elif cause == "ungrounded_answer":
            add(_prompt_candidate(cause, "Tell the model to answer only from the retrieved text",
                                  f"{share} gave answers the retrieved text does not support.", prompt, GROUNDING_RULE))
            t = _first(cfg, ("temperature",))
            if t and float(cfg[t]) > 0:
                add(_cfg_candidate(cause, f"Make answers more deterministic (temperature: {cfg[t]} -> 0)",
                                   f"{share} gave unsupported answers; a lower temperature reduces invented detail.", cfg, t, 0))
        elif cause == "empty_output":
            add(_prompt_candidate(cause, "Require a final text answer",
                                  f"{share} finished without any output.", prompt, COMPLETE_RULE))
        elif cause == "tool_timeout":
            t = _first(cfg, _TIMEOUT)
            if t:
                add(_cfg_candidate(cause, f"Allow tools more time ({t}: {cfg[t]} -> {cfg[t] * 2})",
                                   f"{share} were tool timeouts.", cfg, t, cfg[t] * 2))
            r = _first(cfg, _RETRIES)
            if r:
                add(_cfg_candidate(cause, f"Retry tool calls once more ({r}: {cfg[r]} -> {int(cfg[r]) + 1})",
                                   f"{share} were tool timeouts, which are often temporary.", cfg, r, int(cfg[r]) + 1))
            if not t and not r:
                add(_manual(cause, "Add a timeout or retry setting for the tool",
                            f"{share} were tool timeouts and the config has no timeout or retry setting.", item.get("advice", [])))
        elif cause == "llm_error" and any(s["name"] == "rate_limit" for s in item.get("subcauses", [])):
            r = _first(cfg, _RETRIES)
            if r:
                add(_cfg_candidate(cause, f"Retry rate-limited model calls ({r}: {cfg[r]} -> {int(cfg[r]) + 1})",
                                   f"{share} were model rate limits.", cfg, r, int(cfg[r]) + 1))
            else:
                add(_manual(cause, "Handle model rate limits", f"{share} were model rate limits.",
                            ["Add retry with backoff around the model call.", "Check your provider quota or move to a higher tier."]))
        else:
            add(_manual(cause, f"Investigate {cause.replace('_', ' ')}", f"{share}; this cause needs a code or infrastructure change, not a setting.",
                        item.get("advice", [])))
    for s in report.get("suspect_sources", []):
        add(_manual("bad_source_data", f"Review the document '{s['source']}'",
                    f"It appears in {s['failed_share']:.0%} of failed runs but only {s['healthy_share']:.0%} of healthy ones.",
                    ["Open the document and check it is current, correct and split into sensible passages.",
                     "Re-ingest or remove it, then replay the regression suite."]))
    out.sort(key=lambda c: (not c["verifiable"]))  # verifiable first; order within each group is by cause frequency
    return out[:max_candidates]
