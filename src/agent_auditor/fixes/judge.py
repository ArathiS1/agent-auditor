"""Decide whether a replay of a fix candidate counts as evidence the fix works.

The regression suite is made of past failures, so a candidate can only be judged on whether it fixes
them. It says nothing about whether the candidate breaks questions that used to work; that needs the
guard comparison in the SDK and then a canary. The verdict names are chosen to say exactly that.
"""
from __future__ import annotations

from typing import Any, Mapping


def judge_replay(replay: Mapping[str, Any], *, baseline: Mapping[str, Any] | None = None, min_fixed_share: float = 0.5) -> dict[str, Any]:
    """Judge a candidate's replay. With `baseline` (the replay of the CURRENT version on the same suite) the question is
    "what did this change fix, and what did it break?", so a change that is irrelevant cannot earn credit for cases that
    already passed. Without a baseline it falls back to counting passes.

       verified   - every case the current version fails now passes, and nothing that passed now fails
       improved   - at least `min_fixed_share` of those cases are fixed and nothing broke
       rejected   - too little fixed, something broke, or there was nothing to fix
       incomplete - some cases were not checked, so nothing can be claimed
    Per-case outcomes are used, not 'newly_failing': that field compares with whichever replay ran last, which may be a
    different candidate."""
    results = list(replay.get("results") or [])
    cand = {r.get("case_id"): r.get("outcome") for r in results}
    total = len(cand) or int((replay.get("summary") or {}).get("total") or 0)
    passed = sum(1 for o in cand.values() if o == "pass")
    failed = sum(1 for o in cand.values() if o == "fail")
    unchecked = total - passed - failed
    out: dict[str, Any] = {"passed": passed, "failed": failed, "unchecked": unchecked, "total": total,
                           "still_failing": sorted(c for c, o in cand.items() if o == "fail")}
    if not total:
        return {**out, "status": "incomplete", "pass_share": 0.0, "reason": "the replay contained no cases"}
    if unchecked:
        return {**out, "status": "incomplete", "pass_share": round(passed / total, 4),
                "reason": f"{unchecked} of {total} case(s) were missing or could not be checked"}
    if baseline is None:
        share = passed / total
        if passed == total:
            status, why = "verified", f"all {total} regression case(s) now pass"
        elif share >= min_fixed_share:
            status, why = "improved", f"{passed} of {total} regression case(s) now pass; {failed} still fail"
        else:
            status, why = "rejected", f"only {passed} of {total} regression case(s) pass"
        return {**out, "status": status, "pass_share": round(share, 4), "reason": why}
    base = {r.get("case_id"): r.get("outcome") for r in (baseline.get("results") or [])}
    base_fail = {c for c, o in base.items() if o == "fail" and c in cand}
    fixed = sorted(c for c in base_fail if cand.get(c) == "pass")
    regressed = sorted(c for c, o in base.items() if o == "pass" and cand.get(c) == "fail")
    share = len(fixed) / len(base_fail) if base_fail else 0.0
    out.update(fixed=fixed, regressed=regressed, baseline_failures=len(base_fail), pass_share=round(share, 4))
    if not base_fail:
        return {**out, "status": "rejected", "reason": "the current version already passes every regression case, so there is nothing for a fix to improve"}
    if regressed:
        return {**out, "status": "rejected", "reason": f"fixes {len(fixed)} case(s) but breaks {len(regressed)} that passed before"}
    if len(fixed) == len(base_fail):
        return {**out, "status": "verified", "reason": f"fixes all {len(base_fail)} case(s) the current version fails, and breaks none"}
    if share >= min_fixed_share:
        return {**out, "status": "improved", "reason": f"fixes {len(fixed)} of {len(base_fail)} failing case(s) and breaks none"}
    return {**out, "status": "rejected", "reason": f"fixes only {len(fixed)} of {len(base_fail)} failing case(s)"}


def judge_guard(comparisons: list[dict[str, Any]], *, max_changed_share: float = 0.2) -> dict[str, Any]:
    """Healthy-input check: how many answers that were fine before changed after the fix.
    `comparisons` items: {"input", "baseline", "candidate", "same": bool, "error": str|None}."""
    n = len(comparisons)
    if not n:
        return {"status": "not_run", "checked": 0, "changed": 0, "errors": 0, "reason": "no healthy inputs were available to compare"}
    errors = sum(1 for c in comparisons if c.get("error"))
    changed = sum(1 for c in comparisons if not c.get("same") and not c.get("error"))
    share = (changed + errors) / n
    status = "ok" if share <= max_changed_share and not errors else "risky"
    return {"status": status, "checked": n, "changed": changed, "errors": errors, "changed_share": round(share, 4),
            "reason": f"{changed} of {n} answers that worked before changed and {errors} raised errors after the fix",
            "examples": [c for c in comparisons if not c.get("same")][:3]}
