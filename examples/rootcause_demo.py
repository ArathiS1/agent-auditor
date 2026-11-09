"""Phase 6 demo: from "something is failing" to "this cause, in this step, since this version".

A support bot ships v1 (a few tool timeouts and rate limits) and then v2. v2 breaks refund questions
(the knowledge base lacks them: empty retrieval) and sometimes overflows the model's context.
Agent Auditor classifies every run's root cause, ranks the causes, and notices which version each
one clusters in.

1) Start the server:   uvicorn agent_auditor.api.main:app --reload
2) Run this script:    python examples/rootcause_demo.py
No server? See how well the classifier does on synthetic failures with known causes:
                       python -m agent_auditor.rootcause.benchmark
"""
import os
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

URL = os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000")
HEADERS = {"X-API-Key": os.environ["AUDITOR_API_KEY"]} if os.environ.get("AUDITOR_API_KEY") else {}
TIMEOUT = "httpx.ReadTimeout: The read operation timed out"
RATE = "openai.RateLimitError: Error code: 429 - Rate limit reached for gpt-4o"
CONTEXT = "openai.BadRequestError: This model's maximum context length is 8192 tokens. However, your messages resulted in 9120 tokens."
GENERAL = ["reset my password", "change my email", "cancel my plan", "download an invoice", "update my card"]
REFUND = ["how do I get a refund", "where is my refund", "dispute a payment charge"]


def make_run(agent: str, version: str, started: datetime, fault: str | None, question: str) -> dict:
    docs = 0 if fault == "empty" else 3
    steps = [
        {"kind": "retrieval", "name": "kb_search", "output": ["Help article"] * docs,
         "attributes": {"num_docs": docs, "scores": [0.82] if docs else []}},
        {"kind": "tool", "name": "payments_api", "status": "error" if fault == "timeout" else "ok",
         "error": TIMEOUT if fault == "timeout" else None},
        {"kind": "llm", "name": "answer", "status": "error" if fault in ("rate", "context") else "ok",
         "error": {"rate": RATE, "context": CONTEXT}.get(fault)},
    ]
    failed = fault in ("timeout", "rate", "context")
    return {
        "agent": agent, "version": version, "environment": "production", "status": "error" if failed else "ok",
        "error": next((s["error"] for s in steps if s.get("status") == "error"), None),
        "input": {"question": question},
        "output": None if failed else ("I could not find anything on that." if docs == 0 else "See: Help article"),
        "started_at": started.isoformat(), "ended_at": (started + timedelta(milliseconds=450)).isoformat(),
        "steps": steps,
    }


def main() -> None:
    random.seed(21)
    agent = f"rootcause-demo-{int(time.time())}"
    now = datetime.now(timezone.utc)
    with httpx.Client(base_url=URL, timeout=60, headers=HEADERS) as http:
        for label in ("v1", "v2"):
            http.post(f"/v1/agents/{agent}/versions", json={"label": label, "prompt": f"demo {label}", "activate": False}).raise_for_status()

        def send(version: str, n: int, start_offset_h: float, faults: list[tuple[str, float]]) -> None:
            for i in range(n):
                started = now - timedelta(hours=start_offset_h) + timedelta(seconds=i * 20)
                roll, fault = random.random(), None
                for name, p in faults:
                    if roll < p:
                        fault = name
                        break
                    roll -= p
                question = random.choice(REFUND if (version == "v2" and random.random() < 0.4) else GENERAL)
                if fault == "empty":
                    question = random.choice(REFUND)
                http.post("/v1/runs", json=make_run(agent, version, started, fault, question)).raise_for_status()

        send("v1", 150, 6, [("timeout", 0.03), ("rate", 0.02)])
        send("v2", 100, 2, [("empty", 0.30), ("context", 0.08), ("timeout", 0.03)])
        print(f"sent 250 production runs for agent '{agent}'")

        report = http.post(f"/v1/agents/{agent}/root-causes", json={}).json()
        print(f"\n{report['failed_runs']} of {report['total_runs']} runs failed ({report['failure_rate']:.0%}). Why:")
        for c in report["causes"]:
            steps = ", ".join(f"{s['name']} x{s['count']}" for s in c["top_steps"]) or "-"
            subs = ", ".join(s["name"] for s in c["subcauses"])
            print(f"  {c['count']:>3}  {c['cause']:<24} {c['share_of_failures']:.0%} of failures   step: {steps}" + (f"   kind: {subs}" if subs else ""))

        assoc = report["version_associations"]
        print(f"\nversion comparison ({assoc['tested']} tests): {assoc.get('note', '')}")
        for a in assoc["associations"]:
            print(f"  {a['cause']} is {a['direction'].replace('_', ' ')} in {a['version']}: "
                  f"{a['rate_in_version']:.0%} of its runs vs {a['rate_elsewhere']:.0%} elsewhere (p={a['p_value']:.1e})")

        top = report["causes"][0]
        print(f"\nTop cause: {top['cause']}. {top['meaning']}")
        for tip in top["advice"]:
            print(f"  - {tip}")
        one = http.get(f"/v1/runs/{top['example_run_ids'][0]}/diagnosis").json()
        print(f"\nExample run {one['run_id'][:8]}: {one['explanation']}  [{one['strength']} evidence]")
        print(f"\nBrowse it at {URL}/docs  (POST /v1/agents/{agent}/root-causes)")


if __name__ == "__main__":
    main()
