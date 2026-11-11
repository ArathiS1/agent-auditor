"""Turn one study's recorded runs and the canary report into a plain markdown write-up. Every number comes from the data;
the wording of the comparison follows rules (see `compare`) rather than taste, and the limits are always printed."""
from __future__ import annotations

import statistics
from math import comb

from agent_auditor.evals.scorers import _NON_ANSWER


def fisher_two_sided(a: int, n1: int, c: int, n2: int) -> float:
    """Two-sided Fisher exact p for a/n1 versus c/n2 (a and c are counts of 'correct')."""
    total, k, n = n1 + n2, a + c, n1
    def pmf(x: int) -> float:
        return comb(k, x) * comb(total - k, n - x) / comb(total, n)
    lo, hi = max(0, n - (total - k)), min(k, n)
    p_obs = pmf(a)
    return min(1.0, sum(pmf(x) for x in range(lo, hi + 1) if pmf(x) <= p_obs * (1 + 1e-9)))


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    answered = [r for r in rows if not r["error"]]
    times = [r["seconds"] for r in rows]
    return {
        "n": n, "correct": sum(1 for r in rows if r["correct"]), "errors": sum(1 for r in rows if r["error"]),
        "non_answers": sum(1 for r in answered if _NON_ANSWER.search(r["answer"] or "") and len((r["answer"] or "").split()) <= 30),
        "median_s": statistics.median(times) if times else 0.0,
    }


def compare(base: dict, cand: dict, verdict: str, p: float) -> str:
    """One sentence on whether the canary and the answer key agree. Only these cases exist."""
    fell = cand["correct"] / max(cand["n"], 1) < base["correct"] / max(base["n"], 1) and p < 0.05
    flagged = verdict in ("rollback", "hold")
    if fell and flagged:
        return "The answer key and the canary agree: the candidate is measurably worse, and the canary did not promote it."
    if fell and not flagged:
        return ("The answer key found a drop that the canary did not flag. Without expected answers on the runs, the canary can only see "
                "errors, speed, retrieval and non-answers, so a candidate that answers confidently and wrongly can pass it. "
                "`answer_judge` is the check meant for this case.")
    if flagged:
        return ("The canary flagged the candidate although the answer key shows no significant drop at this sample size. "
                "See the reasons above for what it measured; it may be speed or non-answers rather than correctness.")
    return "Neither the answer key nor the canary found a significant difference at this sample size."


def build(study: dict) -> str:
    base, cand = summarize(study["baseline_rows"]), summarize(study["candidate_rows"])
    p = fisher_two_sided(base["correct"], base["n"], cand["correct"], cand["n"])
    rep = study["canary"]
    pct = lambda s: f"{s['correct']}/{s['n']} ({100 * s['correct'] / max(s['n'], 1):.0f}%)"  # noqa: E731
    out = [f"# Case study: {study['agent']} agent, {study['baseline']} vs {study['candidate']}", ""]
    if study["fake"]:
        out += ["> **DRY RUN WITH A SCRIPTED FAKE MODEL.** These numbers describe the script in `studies/fake_server.py`, not any real model. "
                "Do not publish them as results.", ""]
    out += [
        "## What was run", "",
        f"- Agent: `{study['agent']}`, a stock agent from its framework with two tools (a country lookup and a calculator). "
        "The agent code does not import Agent Auditor.",
        "- Instrumentation: the framework's own OpenInference instrumentor, exporting OpenTelemetry spans to Agent Auditor's `/v1/traces`.",
        f"- Baseline: `{study['baseline']}` (production). Candidate: `{study['candidate']}` (canary). {study['n']} questions each, the same questions in the same order.",
        f"- Questions: {study['n']} arithmetic and lookup questions with one right answer each. Date: {study['date']}.", "",
        "## What the answer key found", "",
        "The runner checks each answer against the known right answer. Agent Auditor never sees these answers: the imported runs carry none.", "",
        "| | baseline | candidate |", "|---|---|---|",
        f"| correct | {pct(base)} | {pct(cand)} |",
        f"| \"I am not sure\"-style non-answers | {base['non_answers']} | {cand['non_answers']} |",
        f"| errors | {base['errors']} | {cand['errors']} |",
        f"| median seconds per run | {base['median_s']:.2f} | {cand['median_s']:.2f} |", "",
        f"Difference in correct answers: Fisher exact test p {'< 0.001' if p < 0.001 else '= ' + format(p, '.3f')}.", "",
        "## What the canary said", "",
        f"Verdict for `{study['candidate']}`: **{rep['verdict'].upper()}**", ""]
    out += [f"- {r}" for r in rep["reasons"]] or ["- (no reasons given)"]
    out += ["", "| check | status | note |", "|---|---|---|"]
    out += [f"| {c['name']} | {c['status']} | {c['reason']} |" for c in rep["checks"]]
    out += ["", "## Do they agree?", "", compare(base, cand, rep["verdict"], p), "",
            "## Limits", "",
            f"- {study['n']} runs per side is a small sample. A real difference can be missed and a chance difference can look real.",
            "- The questions are easy and checkable on purpose. A model's behaviour on your own traffic can differ.",
            "- This measures one agent, one task set and one run of each model. Models change; so do providers' endpoints.",
            "- Correctness was scored by substring and number matching, which can mark a right answer wrong or a wrong one right.",
            "- This says nothing about the quality of the framework used. It is a stock agent used as a realistic test subject.", ""]
    return "\n".join(out)
