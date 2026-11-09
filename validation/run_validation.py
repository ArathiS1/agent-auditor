"""Check Agent Auditor against an agent whose faults are known.

  python validation/run_validation.py --dry-run          no server needed; uses the project's own code in this process
  python validation/run_validation.py                    against a running server (AUDITOR_URL, AUDITOR_API_KEY)
  python validation/run_validation.py --quick            fewer runs (about a minute)
  python validation/run_validation.py --score-labels FILE   compare your hand labels with the tool's

Results go to validation/results/<time>/ (report.md, results.json, labeling_sheet.csv).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import support_bot as A  # noqa: E402
import harness as H  # noqa: E402
import report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="evaluate in this process, no server")
    ap.add_argument("--url", default=os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--quick", action="store_true", help="150 baseline runs, 60 per canary, 3 reps, 5 control reps")
    ap.add_argument("--baseline-runs", type=int, default=300)
    ap.add_argument("--canary-runs", type=int, default=80)
    ap.add_argument("--reps", type=int, default=5, help="canaries per fault")
    ap.add_argument("--control-reps", type=int, default=10, help="canaries of the harmless change")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--llm", action="store_true", help="answer with the Claude API (needs ANTHROPIC_API_KEY; costs money)")
    ap.add_argument("--yes", action="store_true", help="skip the cost confirmation for --llm")
    ap.add_argument("--out", default=None)
    ap.add_argument("--score-labels", metavar="FILE")
    a = ap.parse_args()

    if a.score_labels:
        r = H.score_manual(a.score_labels)
        if not r["labelled"]:
            print("No rows have a your_label yet.")
            return 1
        lo, hi = r["wilson"]
        print(f"You labelled {r['labelled']} of {r['of']}. The tool agreed with you on {r['agree']} ({r['agree'] / r['labelled']:.0%}, 95% interval {lo:.0%}-{hi:.0%}).")
        for n, tool, you in r["disagreements"]:
            print(f"  row {n}: tool said {tool}, you said {you}")
        if r["unknown_labels"]:
            print("Not recognised (check spelling):", ", ".join(r["unknown_labels"]))
        return 0

    if a.quick:
        a.baseline_runs, a.canary_runs, a.reps, a.control_reps = 150, 60, 3, 5
    llm = None
    if a.llm:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            print("--llm needs ANTHROPIC_API_KEY in the environment.")
            return 2
        calls = a.baseline_runs + a.canary_runs * (5 * a.reps + a.control_reps)
        print(f"--llm makes up to {calls} Claude API calls.")
        if not a.yes and input("Continue? [y/N] ").strip().lower() != "y":
            return 0
        llm = A.claude_answerer(key)

    agent_name = "validation-offline" if a.dry_run else f"validation-{int(time.time())}"
    plan = H.make_plan(agent=agent_name, baseline_runs=a.baseline_runs, canary_runs=a.canary_runs, reps=a.reps,
                       control_reps=a.control_reps, seed=a.seed, llm=llm)
    total = len(plan.baseline) + sum(len(c.records) for c in plan.canaries)
    print(f"Planned {total} runs: {len(plan.baseline)} baseline and {len(plan.canaries)} canaries.")
    started = time.time()
    if a.dry_run:
        print("Evaluating in this process (no server) ...")
        H.evaluate_offline(plan)
    else:
        print(f"Sending to {a.url} as agent '{agent_name}' ...")
        try:
            H.evaluate_server(plan, a.url, os.environ.get("AUDITOR_API_KEY"))
        except Exception as exc:  # noqa: BLE001 - show a readable reason instead of a stack trace
            print(f"\nCould not finish: {type(exc).__name__}: {exc}")
            print("Is the server running, and is AUDITOR_API_KEY set the same way as for the server?")
            return 1
    summary = H.summarize(plan)
    out = Path(a.out) if a.out else HERE / "results" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    proves = ("This was an in-process check of the project's pure logic. The API, database and dashboard were not exercised; run it again without --dry-run for that."
              if a.dry_run else f"The server, database and API were exercised end to end. Open {a.url}/dashboard and pick '{agent_name}' to see the same runs.")
    meta = {"agent": agent_name, "mode": "dry run (no server)" if a.dry_run else f"server at {a.url}",
            "answers": "Claude API" if a.llm else "retrieval only (best passage)", "seed": a.seed, "canary_runs": a.canary_runs, "proves": proves}
    (out / "report.md").write_text(report.render(summary, meta), encoding="utf-8")
    (out / "results.json").write_text(json.dumps({"meta": meta, "summary": summary, "canaries": [
        {"label": c.label, "scenario": c.scenario.name, "verdict": c.result["verdict"], "reasons": c.result["reasons"],
         "accuracy": H.accuracy(c.records)} for c in plan.canaries]}, indent=2, default=str), encoding="utf-8")
    n = H.write_labeling_sheet(plan, out / "labeling_sheet.csv")
    print(f"\nDone in {time.time() - started:.0f}s. Report: {out / 'report.md'}  ({n} rows in labeling_sheet.csv)")
    sc = summary["scenarios"]
    print("\nQuick view:")
    for name, v in sc.items():
        print(f"  {name:<11} rollback {v['rollback']}/{v['reps']}  promote {v['promote']}/{v['reps']}  (wanted: {v['expect']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
