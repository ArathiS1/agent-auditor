"""Run three real canary checks, one for each outcome, and write a report you can publish.

  python bench/verdicts.py
  python bench/verdicts.py --fast indos:Deepseek/deepseek-v4-flash --slow airouter:openai/gpt-5-nano --pause 1

The three checks, all on real models with the same questions:
  1. HOLD      the fast model is production, the slow model is the candidate. Nothing is broken, it is just slower.
  2. ROLLBACK  the fast model is production, and the candidate is the same model reached through a wrong address.
               This is a planted fault (a stand-in for a bad config push), not a model that failed by itself.
  3. PROMOTE   the slow model is production, and the fast model is the candidate: a real improvement.

The Auditor decides each verdict from the runs. Whatever it answers is written down, including when it is not what
you hoped for. Each verdict is also checked a second time under a strict policy (any warning blocks a promote), using
the same runs, so you can see where a lenient default would let a weak candidate through.
  python bench/verdicts.py --recheck     rebuild the report from the runs already recorded (no model calls)
The report is saved to docs/real-verdicts.md. The runs go to their own agent ("verdict-lab"), so your
chat agent's numbers stay clean. Keep the Auditor running (run-all.ps1) in another window. About 15 to 25 minutes.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "bench"), str(ROOT / "chatbot"), str(ROOT / "src")]

import canary  # noqa: E402

OUT = ROOT / "docs" / "real-verdicts.md"


def arm_stats(url: str, key: str, agent: str, version: str, environment: str) -> dict:
    """Answer times for one side of a comparison, read back from the Auditor (not from this script's own timer)."""
    import httpx

    r = httpx.get(f"{url}/v1/runs", params={"agent": agent, "version": version, "environment": environment, "limit": 200},
                  headers={"X-API-Key": key}, timeout=30)
    r.raise_for_status()
    runs = r.json()
    times = sorted(x["latency_ms"] / 1000 for x in runs if x.get("latency_ms") is not None)
    failed = sum(1 for x in runs if x["status"] != "ok")
    if not times:
        return {"runs": len(runs), "failed": failed, "median": None, "p95": None}
    return {"runs": len(runs), "failed": failed, "median": statistics.median(times),
            "p95": times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))]}


def strict_verdict(aud, agent: str, baseline: str, candidate: str, runs: int, since_hours: float) -> dict:
    """The same runs judged again with warnings_block on. Nothing is saved and no model is called."""
    return aud.evaluate_canary(
        baseline=baseline, candidate=candidate, agent=agent, since_hours=since_hours, evaluate_first=False, diagnose_first=False,
        save=False, policy={"min_canary_runs": runs, "min_baseline_runs": runs, "warnings_block": True})


def fmt(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds:.1f} s"


def scenario(title: str, expect: str, argv: list[str], url: str, key: str) -> dict | None:
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)
    rc = canary.main(argv)
    if rc != 0 or not canary.LAST:
        return None
    last = dict(canary.LAST)
    from agent_auditor.sdk import Auditor

    aud = Auditor(url, agent=last["agent"], api_key=key)
    last.update(title=title, expect=expect,
                base=arm_stats(url, key, last["agent"], last["baseline"], "production"),
                cand=arm_stats(url, key, last["agent"], last["candidate"], "canary"),
                strict=strict_verdict(aud, last["agent"], last["baseline"], last["candidate"], last["runs"], last["since_hours"]))
    return last


def recheck(args: argparse.Namespace) -> list[dict]:
    """Rebuild the results from the canary reports the Auditor already holds: the newest three different pairs."""
    import httpx
    from agent_auditor.sdk import Auditor

    aud = Auditor(args.auditor, agent=args.agent, api_key=args.key)
    pairs, seen = [], set()
    for r in aud.list_canary_reports(agent=args.agent, limit=50):  # newest first
        k = (r["baseline"], r["candidate"])
        if k not in seen:
            seen.add(k)
            pairs.append(r)
    pairs = pairs[:3][::-1]
    versions = httpx.get(f"{args.auditor}/v1/agents/{args.agent}/versions", headers={"X-API-Key": args.key}, timeout=30).json()
    cfg = {v["label"]: v.get("config") or {} for v in versions}
    out = []
    for r in pairs:
        b, c = r["baseline"], r["candidate"]
        base = arm_stats(args.auditor, args.key, args.agent, b, "production")
        cand = arm_stats(args.auditor, args.key, args.agent, c, "canary")
        report = aud.evaluate_canary(baseline=b, candidate=c, agent=args.agent, since_hours=args.since_hours, evaluate_first=False,
                                     diagnose_first=False, save=False, policy={"min_canary_runs": args.runs, "min_baseline_runs": args.runs})
        bc, cc = cfg.get(b, {}), cfg.get(c, {})
        planted = str(cc.get("provider", "")).endswith("-misconfigured")
        if planted:
            title, expect = "Rollback: a broken candidate", "rollback"
        elif (cand["median"] or 0) > (base["median"] or 0):
            title, expect = "Hold: a slower candidate", "hold (nothing broke, it is slower)"
        else:
            title, expect = "Promote: a faster candidate", "promote (it is faster)"
        out.append(dict(
            title=title, expect=expect, report=report, base=base, cand=cand, planted_fault=planted, agent=args.agent, runs=args.runs,
            base_model=f"{bc.get('provider', '?')}:{bc.get('model', '?')}", cand_model=f"{cc.get('provider', '?')}:{cc.get('model', '?')}",
            answered=(base["runs"] - base["failed"], base["failed"], cand["runs"] - cand["failed"], cand["failed"]),
            strict=strict_verdict(aud, args.agent, b, c, args.runs, args.since_hours)))
    return out


def write_report(results: list[dict], args: argparse.Namespace) -> None:
    lines = [
        "# Real canary verdicts",
        "",
        f"Run on {datetime.now(timezone.utc):%d %B %Y}, with real models and {args.runs} questions per side. "
        "The Auditor decided each verdict from the recorded runs; nothing here was edited by hand.",
        "",
        "| Check | Baseline (production) | Candidate (canary) | Median answer time | 95th percentile | Verdict | Verdict if any warning blocks | What I expected |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        verdict = str(r["report"].get("verdict", "?")).upper()
        b, c = r["base"], r["cand"]
        lines.append(
            f"| {r['title']} | `{r['base_model']}` | `{r['cand_model']}`{' (planted fault)' if r['planted_fault'] else ''} "
            f"| {fmt(b['median'])} vs {fmt(c['median'])} | {fmt(b['p95'])} vs {fmt(c['p95'])} | **{verdict}** | {str(r['strict'].get('verdict', '?')).upper()} | {r['expect']} |")
    lines += ["", "## How each verdict was reached", ""]
    for r in results:
        lines += [f"### {r['title']}: {str(r['report'].get('verdict', '?')).upper()}", ""]
        for reason in r["report"].get("reasons", []):
            lines.append(f"- {reason}")
        lines += ["", "| Check | Result | Detail |", "|---|---|---|"]
        for ch in r["report"].get("checks", []):
            lines.append(f"| {ch.get('name', '?')} | {ch.get('status', '?')} | {ch.get('reason', '')} |")
        warns = r["report"].get("warnings") or []
        if warns:
            lines += ["", "Warnings the Auditor raised (they do not block a promote under the default policy):", ""] + [f"- {w}" for w in warns]
        a = r["answered"]
        lines += ["", f"Answered / errored: baseline {a[0]} / {a[1]}, candidate {a[2]} / {a[3]}.", ""]
    lines += [
        "## What the two verdict columns mean",
        "",
        "The default policy fails a check only when a result is clearly worse (for example latency of 2x the baseline or more). "
        "A smaller change is a **warning**, and warnings alone do not stop a promote. The second column judges the same runs again "
        "with every warning treated as a block. Where the two columns differ, the default let a weaker candidate through.",
        "",
        "## Read this before quoting it",
        "",
        "- The rollback check uses a **planted fault**: the same model reached through a wrong address, so every call fails. "
        "It shows that the Auditor stops a broken release, not that a real model broke by itself.",
        "- The canary judges errors, speed, the built-in quality checks and failure causes. It cannot tell that an answer is "
        "wrong but well formed.",
        f"- {args.runs} questions per side is a small sample. Model speed also varies with the time of day and the provider's load.",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fast", default="indos:Deepseek/deepseek-v4-flash", help="the quicker model")
    ap.add_argument("--slow", default="airouter:openai/gpt-5-nano", help="the slower model")
    ap.add_argument("--runs", type=int, default=24)
    ap.add_argument("--pause", type=float, default=1.0)
    ap.add_argument("--agent", default="verdict-lab")
    ap.add_argument("--since-hours", type=float, default=6.0)
    ap.add_argument("--recheck", action="store_true", help="rebuild the report from runs already recorded; no model calls")
    ap.add_argument("--auditor", default=os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--key", default=os.environ.get("AUDITOR_API_KEY", ""))
    args = ap.parse_args(argv)
    if not args.key:
        print("AUDITOR_API_KEY is not set (it is in .env after the first run-all.ps1).")
        return 2

    if args.recheck:
        results = recheck(args)
        if not results:
            print(f"No canary reports found for agent '{args.agent}'. Run python bench/verdicts.py first.")
            return 1
        write_report(results, args)
        for r in results:
            print(f"  {r['title']:<36} -> {str(r['report'].get('verdict', '?')).upper():<9} strict: {str(r['strict'].get('verdict', '?')).upper()}")
        print(f"\nReport saved to {OUT.relative_to(ROOT)}.")
        return 0

    common = ["--runs", str(args.runs), "--pause", str(args.pause), "--agent", args.agent, "--auditor", args.auditor, "--key", args.key,
              "--since-hours", str(args.since_hours)]
    plan = [
        ("1. Hold: a slower candidate", "hold (nothing broke, it is slower)",
         ["--baseline", args.fast, "--candidate", args.slow] + common),
        ("2. Rollback: a broken candidate", "rollback",
         ["--baseline", args.fast, "--candidate", args.fast, "--break-candidate", "--reuse-baseline"] + common),
        ("3. Promote: a faster candidate", "promote (it is faster)",
         ["--baseline", args.slow, "--candidate", args.fast] + common),
    ]
    results = []
    for title, expect, argv_i in plan:
        got = scenario(title, expect, argv_i, args.auditor, args.key)
        if got is None:
            print(f"\nStopped at '{title}'. Fix the problem shown above and run the script again.")
            break
        results.append(got)
    if results:
        write_report(results, args)
        print("\n" + "=" * 78 + "\nSUMMARY")
        for r in results:
            print(f"  {r['title']:<36} -> {str(r['report'].get('verdict', '?')).upper():<9} strict: {str(r['strict'].get('verdict', '?')).upper():<9} (expected: {r['expect']})")
        print(f"\nReport saved to {OUT.relative_to(ROOT)}. Open {args.auditor}/dashboard#/canary and pick the agent '{args.agent}' to see them.")
    return 0 if len(results) == len(plan) else 1


if __name__ == "__main__":
    raise SystemExit(main())
