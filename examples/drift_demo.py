"""Phase 4 demo: detect drift when no new version was deployed.

A support bot behaves normally for a week (the baseline). Over the last 24 hours the questions
change (refund and chargeback requests appear), retrieval gets worse, answers get slower and a new
tool starts being called. Agent Auditor compares the last day with the week before it.

1) Start the server:   uvicorn agent_auditor.api.main:app --reload
2) Run this script:    python examples/drift_demo.py
Each run uses a fresh agent name, so earlier runs of the demo never contaminate the baseline.
"""
import os
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

URL = os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000")
HEADERS = {"X-API-Key": os.environ["AUDITOR_API_KEY"]} if os.environ.get("AUDITOR_API_KEY") else {}
BASE_TOPICS = [("reset", "password"), ("change", "email"), ("cancel", "plan"), ("invoice", "copy"), ("update", "card")]
NEW_TOPICS = [("refund", "payment"), ("chargeback", "dispute")]


def make_payload(agent: str, started: datetime, drifted: bool) -> dict:
    new_topics = drifted and random.random() < 0.6
    topic = random.choice(NEW_TOPICS if new_topics else BASE_TOPICS)
    latency = max(50.0, random.gauss(1700, 250) if drifted else random.gauss(900, 150))
    missed = random.random() < (0.25 if drifted else 0.03)
    score = round(min(0.99, max(0.05, random.gauss(0.55 if drifted else 0.82, 0.08))), 3)
    tool = "issue_refund" if (drifted and random.random() < 0.35) else random.choice(["lookup_account", "send_email"])
    return {
        "agent": agent, "version": "v1", "environment": "production", "status": "ok",
        "input": f"How do I {topic[0]} my {topic[1]}?",
        "output": "I could not find that." if missed else f"To {topic[0]} your {topic[1]}, open the help article.",
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(milliseconds=latency)).isoformat(),
        "steps": [
            {"kind": "retrieval", "name": "kb_search", "output": [] if missed else ["Help article", "Account overview"],
             "attributes": {"scores": [] if missed else [score]}},
            {"kind": "tool", "name": tool},
            {"kind": "llm", "name": "answer"},
        ],
    }


def main() -> None:
    random.seed(11)
    agent = f"drift-demo-{int(time.time())}"
    now = datetime.now(timezone.utc)
    with httpx.Client(base_url=URL, timeout=30, headers=HEADERS) as http:
        http.post(f"/v1/agents/{agent}/versions", json={"label": "v1", "prompt": "demo", "config": {}}).raise_for_status()

        ref_start, n_ref = now - timedelta(hours=192), 280  # the week before the last day
        for i in range(n_ref):
            http.post("/v1/runs", json=make_payload(agent, ref_start + timedelta(hours=168 * i / n_ref), False)).raise_for_status()
        cur_start, n_cur = now - timedelta(hours=24), 60  # the last 24 hours
        for i in range(n_cur):
            http.post("/v1/runs", json=make_payload(agent, cur_start + timedelta(hours=23.5 * i / n_cur, minutes=1), True)).raise_for_status()
        print(f"sent {n_ref} baseline runs and {n_cur} recent runs for agent '{agent}'")

        scored = http.post("/v1/evals/run", json={"agent": agent, "limit": 500}).json()
        print("scored runs:", scored["evaluated_runs"])

        report = http.post("/v1/drift/check", json={"agent": agent}).json()

    print(f"\noverall: {report['overall'].upper()}   (baseline {report['reference_runs']} runs, recent {report['current_runs']} runs)")
    for s in report["signals"]:
        if s["severity"] in ("low", "medium", "high"):
            print(f"  [{s['severity']:<6}] {s['name']:<24} {s['reason']}")
    quiet = [s["name"] for s in report["signals"] if s["severity"] == "none"]
    print("  unchanged:", ", ".join(quiet) or "-")
    for note in report["notes"]:
        print("  note:", note)
    print(f"\nSaved as report {report['id']}.  Browse: http://127.0.0.1:8000/docs  (GET /v1/agents/{agent}/drift)")


if __name__ == "__main__":
    main()
