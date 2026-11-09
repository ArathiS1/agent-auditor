"""Propose fixes for an agent's failures and prove each one against the regression suite before suggesting it.

    result = auditor.verify_fixes(build_agent)          # build_agent(prompt=..., config=...) -> your agent function

What happens, in order:
  1. The server reads this agent's recent failures, finds the root causes and proposes small candidate fixes
     (one change each: a setting, or one added prompt rule). Nothing is changed yet.
  2. Each candidate is registered as its own non-active version ("fix-<id>-<base>").
  3. The current version is replayed on every regression case (your real past failures) as a baseline. Then your agent
     is rebuilt with each candidate's prompt/config and replayed. The server grades each case, and a candidate is
     judged on what it fixes and what it breaks compared with the baseline.
  4. Optionally, the candidate also answers recent questions that worked before; answers that changed a lot are
     flagged, so a fix that "cures" the failures by breaking everything else does not look good.
  5. You get back only what passed, ranked, plus manual items (code bugs, suspect documents) that cannot be
     checked this way.

What this does NOT do: activate anything, or prove the fix is safe in production. The suite holds past failures
only, and the comparison of old answers is a rough similarity check. The next step is a canary and an approval.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from ..fixes.judge import judge_guard, judge_replay
from ..versioning import content_hash

DEFAULT_SIMILARITY = 0.5


def _words(x: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(x if x is not None else "").lower()))


def similar(a: Any, b: Any, threshold: float = DEFAULT_SIMILARITY) -> bool:
    """Rough 'same answer' test: word overlap (Jaccard) at or above the threshold."""
    wa, wb = _words(a), _words(b)
    if not wa and not wb:
        return True
    return len(wa & wb) / len(wa | wb) >= threshold


def run_verification(
    auditor: Any, build: Callable[..., Callable], *, agent: str, proposal: dict[str, Any],
    guard_runs: list[dict[str, Any]], guard_same: Callable[[Any, Any], bool] | None,
    call: Callable[[Any], Any] | None, min_fixed_share: float,
) -> dict[str, Any]:
    base = proposal["base_version"]
    base_prompt, base_config = base.get("prompt"), dict(base.get("config") or {})
    base_hash = content_hash(base_prompt, base_config)[:6]
    same = guard_same or similar
    results: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    notes = list(proposal.get("notes", []))
    baseline = None
    if any(c.get("verifiable") for c in proposal["candidates"]):
        # Replay the CURRENT version first, so each candidate is judged on what it changes, not on cases that already pass.
        try:
            baseline = auditor.replay_tests(build(prompt=base_prompt, config=base_config), version=base["label"], agent=agent,
                                            label="fix:baseline", call=call)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"baseline replay of '{base['label']}' failed ({type(exc).__name__}: {exc}); candidates were judged on pass count alone")
    for cand in proposal["candidates"]:
        if not cand.get("verifiable"):
            manual.append(cand)
            continue
        prompt = cand["prompt"] if cand.get("prompt") is not None else base_prompt
        config = cand["config"] if cand.get("config") is not None else base_config
        label = f"fix-{cand['id']}-{base_hash}"
        item: dict[str, Any] = {"candidate": cand, "label": label}
        try:
            auditor.register_version(label, prompt, config, activate=False, use=False, agent=agent)
            fn = build(prompt=prompt, config=config)
            replay = auditor.replay_tests(fn, version=label, agent=agent, label=f"fix:{cand['id']}", call=call)
            judgement = judge_replay(replay, baseline=baseline, min_fixed_share=min_fixed_share)
            comparisons: list[dict[str, Any]] = []
            if judgement["status"] in ("verified", "improved"):
                for gr in guard_runs:
                    row = {"input": gr["input"], "baseline": gr.get("output"), "candidate": None, "same": False, "error": None}
                    try:
                        row["candidate"] = auditor._call_in_test(fn, gr["input"], version=label, agent=agent, call=call,
                                                                 tags={"guard": True})
                        row["same"] = bool(same(gr.get("output"), row["candidate"]))
                    except Exception as exc:  # noqa: BLE001 - a crash on a healthy input is exactly what we want to see
                        row["error"] = f"{type(exc).__name__}: {exc}"[:200]
                    comparisons.append(row)
                auditor.flush(30.0)
            guard = judge_guard(comparisons)
            item.update(replay_id=replay.get("id"), judgement=judgement, guard=guard)
            if judgement["status"] in ("verified", "improved"):
                item["status"] = "recommended" if guard["status"] in ("ok", "not_run") else "risky"
            else:
                item["status"] = judgement["status"]
        except Exception as exc:  # noqa: BLE001 - one broken candidate must not stop the others
            item.update(status="error", error=f"{type(exc).__name__}: {exc}"[:300])
        results.append(item)
    order = {"verified": 0, "improved": 1}
    ranked = sorted((r for r in results if r["status"] == "recommended"),
                    key=lambda r: (order.get(r["judgement"]["status"], 9), -r["judgement"]["pass_share"], r["candidate"]["id"]))
    return {
        "agent": agent, "base_version": base["label"], "failed_runs": proposal.get("failed_runs", 0),
        "recommended": [r["label"] for r in ranked], "results": results, "manual": manual,
        "suspect_sources": proposal.get("suspect_sources", []), "notes": notes,
        "next_step": ("Nothing has been activated. Send a small share of traffic to the first recommended version with "
                      "canary_router, run evaluate_canary, and release it only through the approval gate."
                      if ranked else
                      "No candidate passed the regression suite, so none is suggested. Read the manual items, or fix the "
                      "agent's code and replay again."),
    }
