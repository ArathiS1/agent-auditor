"""Send every question in questions.csv through the notes bot, traced by Agent Auditor.

Typical use (server running):
  python realbot/run_questions.py                                      # v1, production: the normal baseline
  python realbot/run_questions.py --version v2 --env canary --evaluate # a healthy copy: expect promote
  python realbot/run_questions.py --version v3 --env canary --break search --evaluate   # a bad release: expect rollback
Add --no-llm to answer without Claude (no API key, no cost).
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from bot import DECLINE, SYSTEM, build_bot  # noqa: E402

from agent_auditor.sdk import Auditor, AuditorError  # noqa: E402


def has(answer: str, keyword: str) -> bool:
    return bool(keyword) and re.search(r"(?<!\w)" + re.escape(keyword) + r"(?!\w)", answer, re.I) is not None


def local_verdict(row: dict, answer: str | None) -> str:
    """The bot's own scorecard, from the expected keyword in questions.csv. The auditor never sees this."""
    if row["in_scope"] == "yes":
        if answer is None:
            return "crashed"
        if answer == DECLINE:
            return "wrongly_declined"
        return "correct" if has(answer, row["expected"]) else "wrong_answer"
    if answer is None:
        return "crashed"
    return "correct_decline" if answer == DECLINE else "answered_off_topic"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--agent", default="notes-bot")
    ap.add_argument("--version", default="v1", help="label for this bot version (v1 = baseline)")
    ap.add_argument("--env", default="production", choices=["production", "canary", "test"])
    ap.add_argument("--questions", type=Path, default=HERE / "questions.csv")
    ap.add_argument("--notes", type=Path, default=HERE / "notes")
    ap.add_argument("--repeat", type=int, default=1, help="send the question list this many times")
    ap.add_argument("--limit", type=int, default=0, help="only the first N questions (quick test)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--top-k", type=int, default=4)
    ap.add_argument("--min-score", type=float, default=0.35)
    ap.add_argument("--no-llm", action="store_true", help="answer with the best matching sentence instead of Claude")
    ap.add_argument("--break", dest="break_mode", choices=["search", "llm"], default=None,
                    help="simulate a bad release: 'search' loses results, 'llm' hits rate-limit errors (35%% of runs)")
    ap.add_argument("--evaluate", action="store_true", help="after sending, ask the server for a canary verdict against v1")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    api_key = os.environ.get("AUDITOR_API_KEY")
    auditor = Auditor(args.url, agent=args.agent, api_key=api_key, environment=args.env)
    bot = build_bot(notes=args.notes, auditor=auditor, use_llm=False if args.no_llm else None, top_k=args.top_k,
                    min_score=args.min_score, break_mode=args.break_mode, seed=args.seed)
    config = {**bot.cfg.describe(), "label": args.version}  # label inside the config keeps each version's content distinct
    try:
        reg = auditor.register_version(args.version, prompt=SYSTEM, config=config,
                                       activate=(args.version == "v1" and args.env == "production"))
    except AuditorError as exc:
        hint = ("\nIs the server running at " + args.url + "?  Did you set AUDITOR_API_KEY to the server's key?"
                if "could not reach" in str(exc) or "401" in str(exc) or "403" in str(exc) else
                f"\nVersion label '{args.version}' already exists with different settings. Use a new --version (v2, v3, ...).")
        raise SystemExit(f"Could not register the version: {exc}{hint}")
    if reg.get("label") and reg["label"] != args.version:
        raise SystemExit(f"The server matched these settings to the existing version '{reg['label']}'. "
                         f"Change a setting (for example --top-k) or use that label.")

    rows = list(csv.DictReader(args.questions.open(encoding="utf-8")))
    if args.limit:
        rows = rows[: args.limit]
    work = rows * args.repeat
    print(f"agent={args.agent} version={args.version} env={args.env} answerer={config['answerer']} "
          f"questions={len(work)} break={args.break_mode or 'none'}")

    started = time.time()

    def one(row: dict) -> tuple[dict, str | None, str | None]:
        try:
            return row, bot.ask(row["question"]), None
        except Exception as exc:  # the failed run is already recorded by the SDK
            return row, None, f"{type(exc).__name__}: {exc}"[:200]

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for i, res in enumerate(pool.map(one, work), 1):
            results.append(res)
            if i % 25 == 0 or i == len(work):
                print(f"  {i}/{len(work)} done ({time.time() - started:.0f}s)")
    auditor.flush(timeout=60)
    print(f"sent to the auditor: {auditor.stats}")

    tally: dict[str, int] = {}
    out_path = HERE / f"last_run_{args.version}.csv"
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["question", "in_scope", "expected", "answer", "error", "bot_scorecard"])
        for row, answer, err in results:
            verdict = local_verdict(row, answer)
            tally[verdict] = tally.get(verdict, 0) + 1
            w.writerow([row["question"], row["in_scope"], row["expected"], answer or "", err or "", verdict])
    total = len(results)
    print("\nThe bot's own scorecard (this is what you know and the auditor does not):")
    for name in ("correct", "correct_decline", "wrongly_declined", "wrong_answer", "answered_off_topic", "crashed"):
        if name in tally:
            print(f"  {name:<20}{tally[name]:>5}  ({100 * tally[name] / total:.0f}%)")
    print(f"  details: {out_path}")

    if args.evaluate:
        import httpx

        headers = {"X-API-Key": api_key} if api_key else {}
        resp = httpx.post(f"{args.url}/v1/agents/{args.agent}/canary/evaluate", headers=headers, timeout=60,
                          json={"baseline": "v1", "candidate": args.version})
        if resp.status_code >= 400:
            raise SystemExit(f"Canary evaluate failed: HTTP {resp.status_code} {resp.text[:300]}")
        report = resp.json()
        print(f"\nCanary verdict for {args.version} against v1:  {report['verdict'].upper()}")
        for reason in report["reasons"]:
            print(f"  - {reason}")
        for c in report["checks"]:
            print(f"  [{c['status']:<7}] {c['name']:<8} {c['reason']}")
    print(f"\nNow open {args.url}/dashboard and pick the agent '{args.agent}'.")


if __name__ == "__main__":
    main()
