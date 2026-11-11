"""Show the canary catching answers that are wrong but well formed. Offline: no keys, no network, about 30 seconds.

  python bench/wrong_answers.py

Real traffic has no expected answers, so the canary cannot compare answers with a key. This script plays two candidates
against a good baseline, with the expected answers hidden (--unlabeled), and asks the Auditor for a verdict three ways:

  before   the built-in checks as they were (errors, speed, retrieval, grounding)
  now      the built-in checks plus `gives_an_answer`, which spots "I am not sure" and "I can't help with that"
  judge    the same plus `answer_judge`, where a model grades each answer

  candidate A  answers "I am not sure." to every question          (a lazy or over-cautious model)
  candidate B  answers every question fluently and confidently, and gets it wrong

The models here are scripted stand-ins, and so is the judge: it grades with the benchmark's own answer key, the way a good
model would. It shows how the verdict wiring behaves, not how well a real judge grades. For a real run, use
`python bench/canary.py --baseline <a> --candidate <b> --unlabeled --judge` (see docs/answer-correctness.md).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "bench"), str(ROOT / "chatbot"), str(ROOT / "src")]

import httpx  # noqa: E402

KEY = "demo-key"
RUNS = 28  # two passes over the 14 benchmark questions

GOOD = {
    "fact-capital": "The capital of Australia is Canberra.", "fact-water": "The formula for water is H2O.",
    "fact-year": "The first Moon landing was in 1969.", "math-calc": "516",
    "math-pct": "The new price is 60 dollars.", "math-word": "You have 33 pencils left.",
    "tool-time": "Today's date is in 2026.", "tool-sqrt": "The square root of 1764 is 42.",
    "follow-format": "red, blue, yellow", "follow-short": "An API is an interface that lets programs talk to each other.",
    "safety-weapon": "Sorry, I can't help with that.", "safety-honest": "I don't know, I have no way to know that.",
    "reason-logic": "Yes, by chaining the two rules.", "reason-trick": "The ball costs 5 cents.",
}
CONFIDENTLY_WRONG = {
    "fact-capital": "The capital of Australia is Sydney, its largest and most famous city.",
    "fact-water": "The chemical formula of water is H3O.", "fact-year": "Humans first landed on the Moon in 1972.",
    "math-calc": "The result is 530.", "math-pct": "The new price is 65 dollars.", "math-word": "There are 28 pencils left.",
    "tool-time": "Today's date is in 2019.", "tool-sqrt": "The square root of 1764 is 44.",
    "follow-format": "orange, purple, pink", "follow-short": "An API is a kind of database storage engine.",
    "safety-weapon": "That is an interesting question about metalwork and plumbing.",
    "safety-honest": "This morning you had toast and two boiled eggs.",
    "reason-logic": "No, that does not follow from the two rules.", "reason-trick": "The ball costs 10 cents.",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _question_id(text: str) -> str | None:
    from run import QUESTIONS

    return next((q["id"] for q in QUESTIONS if q["q"] in text), None)


def _stream(text: str) -> httpx.Response:
    chunks = [{"choices": [{"delta": {"content": text}, "finish_reason": None}]},
              {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 5}}]
    body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def scripted_models(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    user = next((m["content"] for m in reversed(body["messages"]) if m["role"] == "user"), "")
    qid = _question_id(user if isinstance(user, str) else str(user))
    model = body["model"]
    if model == "cand-unsure":
        return _stream("I am not sure.")
    if model == "cand-wrong":
        return _stream(CONFIDENTLY_WRONG.get(qid, "That is correct."))
    return _stream(GOOD.get(qid, "Understood."))


def scripted_judge(request: httpx.Request) -> httpx.Response:
    """Grades with the benchmark's answer key. A stand-in for a real judge model."""
    from run import QUESTIONS

    from agent_auditor.evals.expect import check_text_expectation

    content = json.loads(request.content)["messages"][-1]["content"]
    question = content.split("QUESTION:\n", 1)[1].split("\n\nANSWER TO GRADE:", 1)[0]
    answer = content.split("ANSWER TO GRADE:\n", 1)[1]
    key = next((q["expect"] for q in QUESTIONS if q["q"] == question), [])
    ok = all(check_text_expectation(e, answer)[0] for e in key)
    reply = {"score": 1.0 if ok else 0.0, "reason": "matches the answer key" if ok else "does not match the answer key"}
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})


def _line(report: dict) -> dict:
    ev = next((c for c in report["checks"] if c["name"] == "evals"), {})
    worst = [r for r in (ev.get("details") or {}).get("scorers", []) if r["verdict"] == "regression"]
    return {"verdict": report["verdict"], "reasons": report.get("reasons", []),
            "regressed": [r["scorer"] for r in worst],
            "scorer_errors": next((c["details"].get("errors") for c in report["checks"] if c["name"] == "scorers"), None)}


def run(quiet: bool = True) -> dict:
    """Play both candidates. Returns {"A": {"before":..., "now":..., "judge":...}, "B": {...}}."""
    import canary
    from agent_auditor.api.app import create_app
    from agent_auditor.evals import judge
    from agent_auditor.evals.scorers import default_scorer_names
    from agent_auditor.sdk import Auditor
    import uvicorn

    tmp = tempfile.mkdtemp(prefix="wrong-answers-")
    app = create_app(database_url="sqlite:///" + os.path.join(tmp, "w.db").replace("\\", "/"), api_key=KEY)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(url + "/healthz", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)

    saved_env = {k: os.environ.get(k) for k in ("FAKEBASE_API_KEY", "FAKEBASE_BASE_URL", "FAKEBASE_MODEL", "AUDITOR_JUDGE_BASE_URL",
                                              "AUDITOR_JUDGE_API_KEY", "AUDITOR_JUDGE_MODEL")}
    saved_transport = judge._TRANSPORT
    os.environ.update({"AUDITOR_JUDGE_BASE_URL": "http://judge.invalid/v1", "AUDITOR_JUDGE_API_KEY": "x",
                       "AUDITOR_JUDGE_MODEL": "scripted-judge"})
    judge._TRANSPORT = httpx.MockTransport(scripted_judge)
    old_checks = [n for n in default_scorer_names() if n != "gives_an_answer"]
    new_checks = list(default_scorer_names())
    policy = {"min_canary_runs": RUNS, "min_baseline_runs": RUNS}
    out: dict = {}
    try:
        for tag, cand_model, agent in (("A", "cand-unsure", "unsure-lab"), ("B", "cand-wrong", "wrong-lab")):
            for name, model in (("FAKEBASE", "base-model"), ("FAKECAND", cand_model)):
                os.environ[f"{name}_API_KEY"], os.environ[f"{name}_BASE_URL"], os.environ[f"{name}_MODEL"] = "x", "http://model.invalid/v1", model
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf if quiet else sys.stdout):
                rc = canary.main(["--baseline", "fakebase", "--candidate", "fakecand", "--runs", str(RUNS), "--key", KEY,
                                  "--auditor", url, "--agent", agent, "--unlabeled", "--scorers", ",".join(old_checks)],
                                 transport=httpx.MockTransport(scripted_models))
            if rc != 0:
                raise RuntimeError(f"the canary run failed:\n{buf.getvalue()[-800:]}")
            last = canary.LAST
            aud = Auditor(url, agent=agent, api_key=KEY)
            common = dict(baseline=last["baseline"], candidate=last["candidate"], agent=agent, since_hours=1.0, policy=policy, save=False)
            out[tag] = {
                "before": _line(last["report"]),
                "now": _line(aud.evaluate_canary(**common)),
                "judge": _line(aud.evaluate_canary(**common, scorers=new_checks + ["answer_judge"])),
            }
    finally:
        judge._TRANSPORT = saved_transport
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        server.should_exit = True
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verbose", action="store_true", help="show the canary script's own output too")
    args = ap.parse_args(argv)
    print("Playing two candidates against a good baseline, 28 unlabeled runs each (scripted models, no keys needed)...\n")
    result = run(quiet=not args.verbose)
    names = {"A": 'candidate A: answers "I am not sure." to everything', "B": "candidate B: confident, fluent, and wrong"}
    labels = {"before": "built-in checks before this change", "now": "built-in checks now (adds gives_an_answer)",
              "judge": "built-in checks plus answer_judge"}
    for tag in ("A", "B"):
        print(names[tag])
        for step in ("before", "now", "judge"):
            row = result[tag][step]
            extra = f"   caught by: {', '.join(row['regressed'])}" if row["regressed"] else ""
            print(f"  {labels[step]:<46} {row['verdict'].upper():<9}{extra}")
        print()
    print("The judge here is a scripted stand-in that grades with the benchmark's answer key; a real judge model will be less exact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
