"""Write the plain-language report from the numbers."""
from __future__ import annotations

from datetime import datetime

from harness import wilson

NAMES = {"tool_timeout": "tool timeout", "llm_error": "model error", "slow_run": "slow run", "retrieval": "retrieval", "stale": "out-of-date fact"}


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def frac(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k}/{n} ({pct(lo)}-{pct(hi)})" if n else "n/a"


def render(summary: dict, meta: dict) -> str:
    S, L = summary, summary["labels"]
    sc = S["scenarios"]
    out = [f"# Validation report: {meta['agent']}", "",
           f"Run on {datetime.now().strftime('%Y-%m-%d %H:%M')}. Mode: **{meta['mode']}**. Agent answers: **{meta['answers']}**. "
           f"Seed {meta['seed']}. {S['baseline_runs']} baseline runs, {meta['canary_runs']} runs per canary, "
           f"{sum(v['reps'] for v in sc.values())} canaries in total.", "",
           "Numbers in brackets are 95% intervals. With only a handful of canaries they are wide: read them as a range of what could be true.", ""]

    out += ["## 1. The agent on its own", "",
            f"- Correct answers (judged against known right values, not by the tool): **{pct(S['baseline_accuracy'])}**.",
            f"- Wrong answers that look fine to the tool (the run finished, no error): **{S['baseline_silent_wrong']}** of {S['baseline_runs']}. "
            "Symptom-based checks cannot see these; only a ground-truth check or an LLM judge can.", ""]

    out += ["## 2. Did the canary catch the planted faults?", "",
            "Each row is a worse version (or, for `control`, a harmless change) tried as a canary several times with different random traffic.", "",
            "| Scenario | What was changed | Wanted | Rollback | Hold | Promote | Caught (rollback) | Right cause named | Agent accuracy |",
            "|---|---|---|---|---|---|---|---|---|"]
    for name, v in sc.items():
        vd = v["verdicts"]
        cause = frac(v["cause_named_ok"], v["rollback"]) if v["rollback"] and v["cause_expected"] else "n/a"
        caught = frac(v["rollback"], v["reps"]) if name != "control" else "n/a"
        out.append(f"| {name} | {v['blurb']} | {v['expect']} | {vd.get('rollback', 0)} | {vd.get('hold', 0)} | {vd.get('promote', 0)} | {caught} | {cause} | {pct(v['accuracy'])} |")
    out += [""]
    ctl = sc["control"]
    fa = ctl["reps"] - ctl["promote"]
    out += ["## 3. False alarms", "",
            f"A harmless change was tried {ctl['reps']} times. The tool said promote {ctl['promote']} times and raised a false alarm "
            f"(hold or rollback) **{frac(fa, ctl['reps'])}** of the time.", ""]

    out += ["## 4. Root-cause labels", "",
            "Because the harness planted every fault, it knows the true cause of each failing run. \"Flagged\" means the tool called the run a failure at all; "
            "\"label right\" means that when it did, it named the correct cause.", "",
            "| True cause | Runs | Flagged as a failure | Label right when flagged | Labels the tool used |", "|---|---|---|---|---|"]
    for eff in ("tool_timeout", "llm_error", "slow_run", "retrieval", "stale"):
        r = L["by_effect"].get(eff)
        if not r:
            continue
        used = ", ".join(f"{k} {n}" for k, n in sorted(r["labels"].items(), key=lambda t: -t[1])) or "none"
        right = frac(r["correct"], r["flagged"]) if eff != "stale" else "n/a (no correct label exists)"
        out.append(f"| {NAMES[eff]} | {r['n']} | {frac(r['flagged'], r['n'])} | {right} | {used} |")
    none = L["by_effect"].get("none")
    if none:
        used = ", ".join(f"{k} {n}" for k, n in sorted(none["labels"].items(), key=lambda t: -t[1])) or "none"
        out.append(f"| no fault planted | {none['n']} | {frac(none['flagged'], none['n'])} | n/a | {used} |")
    out += ["", f"Across the causes the classifier can see (timeout, model error, slow run, retrieval), it named the right cause for **{frac(L['labelable_correct'], L['labelable_n'])}** of all affected runs (runs it did not flag at all count as misses).", ""]
    if none and none["flagged"]:
        out += [f"The {none['flagged']} flagged runs with no planted fault are mostly out-of-scope questions (nothing in the help center matches) and "
                "weak matches. The rule-based classifier reports those as retrieval problems. That is a fair reading of the symptom, but it is also noise a person has to learn to ignore.", ""]

    blind = [n for n, v in sc.items() if n != "control" and v["rollback"] < v["reps"]]
    out += ["## 5. What it missed", ""]
    if blind:
        for n in blind:
            v = sc[n]
            why = ("The right article was found with a high score; only the fact inside it was wrong, and nothing in the built-in checks looks at facts."
                   if n == "stale" else
                   "Missing articles were replaced by other plausible articles with decent scores, so retrieval looked healthy while answers were wrong.")
            out.append(f"- **{n}**: rolled back {v['rollback']} of {v['reps']} times (accuracy fell to {pct(v['accuracy'])}). {why}")
        out += ["", "These are the cases that need a ground-truth check, a regression suite built from real failures, or an LLM judge. Reporting them is part of the result, not a flaw in the report.", ""]
    else:
        out += ["Every planted fault was rolled back in every repetition.", ""]

    out += ["## 6. What this does and does not show", "",
            "- It shows the scoring, root-cause and canary logic behave sensibly against faults whose true cause is known, and how often they are wrong.",
            "- The faults are planted and simple. Real failures are messier, so treat these rates as a best case, not a forecast.",
            "- Durations are simulated unless noted, and the agent's answers come from a retrieval-only bot unless you used `--llm`.",
            f"- {meta['proves']}", ""]

    out += ["## 7. Time to find the cause (fill in by hand)", "",
            "Pick one failing version from the table. Start a timer, then answer: *why is it failing?*", "",
            "| Method | Minutes to a confident answer | What you looked at |", "|---|---|---|",
            "| Reading raw logs / run data yourself | | |", "| Using the dashboard (banner, Why runs fail, canary) | | |", ""]
    out += ["## 8. Check the labels yourself", "",
            "Open `labeling_sheet.csv`, fill the `your_label` column for each row using only the question, answer and steps, then run:",
            "", "```", "python validation/run_validation.py --score-labels validation/results/<this run>/labeling_sheet.csv", "```", "",
            "Allowed labels: " + ", ".join(sorted(__import__('agent_auditor.rootcause.classify', fromlist=['CAUSES']).CAUSES)) + ", none.", ""]
    return "\n".join(out)
