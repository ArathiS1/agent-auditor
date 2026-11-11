"""Produce a real canary verdict: send the same questions to two models, score them, and ask Agent Auditor what to do.

  python bench/canary.py --list
  python bench/canary.py --baseline indos:Deepseek/deepseek-v4-flash --candidate airouter:openai/gpt-5-nano
  python bench/canary.py --baseline gemini --candidate mistral --runs 24 --pause 2
  python bench/canary.py --baseline gemini --candidate mistral --reuse-baseline    (the baseline was already run)

Each model is its own version of "chat-agent". The baseline's runs go to the "production" environment and the
candidate's to "canary", the way a real canary split would record them. Then the Auditor compares error rate,
quality checks, speed and failure causes and answers: promote, hold, rollback or continue.
The verdict is saved, so it appears on the dashboard's Canary page (and in the audit log).

A verdict needs at least --runs runs on both sides (default 24: the 14 benchmark questions, repeated).
Fewer than that and the Auditor correctly answers "continue" and explains why.

What the verdict can and cannot see: errors, speed, the built-in quality checks and failure causes. It cannot tell that
a model gave a wrong but well-formed answer; compare answers with `python bench/run.py` (the Benchmark page) as well.
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "chatbot"), str(ROOT / "src"), str(ROOT / "bench")]

from envfile import load_env  # noqa: E402

load_env(ROOT / "chatbot")

from run import QUESTIONS, choose_models  # noqa: E402

LAST: dict = {}  # the newest run's labels and verdict report, read by bench/verdicts.py


def pick_model(wanted: str):
    """One ready model from a provider name ('gemini') or an id ('indos:Deepseek/deepseek-v4-flash')."""
    from providers import provider_for

    models = choose_models([wanted])
    exact = [m for m in models if m["id"].lower() == wanted.lower()]
    chosen = (exact or models or [None])[0]
    if chosen is None:
        # an id for a model that is not the provider's default is still fine to try
        p = provider_for(wanted)
        return p if p is not None and p.ready else None
    return provider_for(chosen["id"])


def drive(agent, store, provider, questions: list[dict], pause: float, label: str, keep_going: bool = False) -> tuple[int, int]:
    ok = bad = streak = 0
    for i, q in enumerate(questions, 1):
        if streak >= 4 and not keep_going:
            print(f"  stopped: 4 errors in a row on {label}. Fix the cause (see check_keys.py) or choose another model.")
            break
        cid = store.create()
        err = ""
        try:
            for ev in agent.turn(cid, q["q"], provider=provider, extra_tags={"canary_demo": True, "question_id": q["id"], "category": q["category"], "expect": q["expect"]}):
                if ev["type"] == "error":
                    err = ev.get("message", "error")
        except Exception as exc:  # noqa: BLE001 - an error is data for the canary, not a reason to stop
            err = f"{type(exc).__name__}: {exc}"
        ok += not err
        bad += bool(err)
        streak = streak + 1 if err else 0
        print(f"  {i:>2}/{len(questions)} {'ok   ' if not err else 'ERROR'} {q['id']}" + (f"  {err[:90]}" if err else ""))
        if pause:
            time.sleep(pause)
    return ok, bad


def main(argv: list[str] | None = None, transport=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", default="", help="the model you trust today: provider or provider:model")
    ap.add_argument("--candidate", default="", help="the model you are thinking of switching to")
    ap.add_argument("--runs", type=int, default=24, help="runs per side (default 24)")
    ap.add_argument("--reuse-baseline", action="store_true",
                    help="do not ask the baseline model again: use the production runs it already has from an earlier run of this script")
    ap.add_argument("--since-hours", type=float, default=3.0, help="how far back the comparison looks (default 3)")
    ap.add_argument("--pause", type=float, default=0.0, help="seconds to wait between questions (helps with rate limits)")
    ap.add_argument("--break-candidate", action="store_true",
                    help="plant a fault: the candidate is the same model reached through a wrong address, so every call fails "
                         "(a stand-in for a bad config push). Use it to see a rollback.")
    ap.add_argument("--list", action="store_true", help="show the models that have a key, then stop")
    ap.add_argument("--auditor", default=os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--key", default=os.environ.get("AUDITOR_API_KEY", ""))
    ap.add_argument("--agent", default=os.environ.get("AUDITOR_AGENT", "chat-agent"))
    args = ap.parse_args(argv)

    if args.list:
        print("Models with a key (use these ids):")
        for m in choose_models([]):
            print("  " + m["id"])
        return 0
    if not args.baseline or not args.candidate:
        print("Choose both: --baseline <model> --candidate <model>. See the ids with: python bench/canary.py --list")
        return 2
    if not args.key:
        print("AUDITOR_API_KEY is not set (it is in .env after the first run-all.ps1).")
        return 2
    if args.runs < 5:
        print("--runs must be at least 5.")
        return 2

    base_p, cand_p = pick_model(args.baseline), pick_model(args.candidate)
    for name, p in (("baseline", base_p), ("candidate", cand_p)):
        if p is None:
            print(f"The {name} model was not found or has no key. See the ids with: python bench/canary.py --list")
            return 2
    if args.break_candidate:
        cand_p = dataclasses.replace(cand_p, name=cand_p.name + "-misconfigured", base_url="http://127.0.0.1:9/v1")
    elif f"{base_p.name}:{base_p.model}" == f"{cand_p.name}:{cand_p.model}":
        print("Baseline and candidate are the same model; the comparison would mean nothing. Choose two different models.")
        return 2

    from agent import ChatAgent
    from agent_auditor.sdk import Auditor
    from store import Store
    from tools import Toolbox
    from tracing import Tracing

    base_aud = Auditor(args.auditor, agent=args.agent, api_key=args.key, environment="production")
    cand_aud = Auditor(args.auditor, agent=args.agent, api_key=args.key, environment="canary")
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    store = Store(Path(tmp.name) / "canary.db")
    toolbox = Toolbox(notes_index=None, store=store)
    kwargs = {"transport": transport} if transport is not None else {}
    base_agent = ChatAgent(store, toolbox, auditor=base_aud, **kwargs)
    cand_agent = ChatAgent(store, toolbox, auditor=cand_aud, **kwargs)
    tracing = Tracing(args.auditor, args.agent, args.key, [t["name"] for t in toolbox.defs], base_agent.provider, auditor=base_aud)
    if not tracing.connect():
        print(f"Cannot reach Agent Auditor at {args.auditor}: {tracing.error}")
        return 2
    base_agent.version_for = cand_agent.version_for = tracing.version_for
    base_label, cand_label = tracing.version_for(base_p), tracing.version_for(cand_p)
    if not base_label or not cand_label:
        print("Could not register the two versions with the Auditor.")
        return 2

    questions = [QUESTIONS[i % len(QUESTIONS)] for i in range(args.runs)]
    print(f"Baseline : {base_p.model} ({base_p.name}), version {base_label}, environment production")
    print(f"Candidate: {cand_p.model} ({cand_p.name}), version {cand_label}, environment canary")
    print(f"{args.runs} questions each.\n")
    if args.reuse_baseline:
        print("Baseline answers: reusing the runs already recorded (the Auditor will say if there are too few).")
        b_ok, b_bad = args.runs, 0
    else:
        print("Baseline answers:")
        b_ok, b_bad = drive(base_agent, store, base_p, questions, args.pause, base_p.model)
    print("\nCandidate answers:")
    c_ok, c_bad = drive(cand_agent, store, cand_p, questions, args.pause, cand_p.model, keep_going=args.break_candidate)
    base_aud.flush(timeout=30)
    cand_aud.flush(timeout=30)
    print(f"\nBaseline: {b_ok} answered, {b_bad} errored. Candidate: {c_ok} answered, {c_bad} errored.")

    short = [n for n, got in (("baseline", b_ok + b_bad), ("candidate", c_ok + c_bad)) if got < args.runs]
    if short:
        print(f"\nNote: the {' and '.join(short)} did not reach {args.runs} runs, so the Auditor will (correctly) refuse to give a "
              "promote or rollback verdict. Fix the cause shown above and run again, or choose another model.")
    report = base_aud.evaluate_canary(
        baseline=base_label, candidate=cand_label, agent=args.agent,
        baseline_environment="production", candidate_environment="canary", since_hours=args.since_hours,
        policy={"min_canary_runs": args.runs, "min_baseline_runs": args.runs},
    )
    LAST.clear()
    LAST.update(report=report, baseline=base_label, candidate=cand_label, agent=args.agent, runs=args.runs,
                since_hours=args.since_hours, base_model=f"{base_p.name}:{base_p.model}", cand_model=f"{cand_p.name}:{cand_p.model}",
                planted_fault=bool(args.break_candidate), answered=(b_ok, b_bad, c_ok, c_bad))
    print(f"\nVERDICT: {str(report.get('verdict', '?')).upper()}")
    for reason in report.get("reasons", []):
        print(f"  - {reason}")
    for c in report.get("checks", []):
        print(f"  [{c.get('status', '?'):>7}] {c.get('name', '?')}: {c.get('reason', '')}")
    print(f"\nOpen {args.auditor}/dashboard#/canary to see it (and #/audit for the record).")
    tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
