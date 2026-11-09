"""Phase 7 demo: should this candidate version be promoted?

v1 is in production. Two candidates each get a slice of live traffic as a canary:
  v2 is slower and times out on the payments API far more often  -> expect rollback
  v3 behaves like v1                                             -> expect promote (or continue)

1) Start the server:   uvicorn agent_auditor.api.main:app --reload
2) Run this script:    python examples/canary_demo.py
The runs are posted directly so the demo is repeatable; in your own code, `auditor.canary_router(...)`
splits live traffic and records each arm for you (see README, Phase 7).
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
QUESTIONS = ["reset my password", "change my email", "cancel my plan", "download an invoice", "update my card"]


def make_run(agent: str, version: str, environment: str, started: datetime, fault: str | None, latency_ms: int) -> dict:
    failed = fault in ("timeout", "rate")
    steps = [
        {"kind": "retrieval", "name": "kb_search", "output": ["Help article"] * 3,
         "attributes": {"num_docs": 3, "scores": [0.82]}},
        {"kind": "tool", "name": "payments_api", "status": "error" if fault == "timeout" else "ok",
         "error": TIMEOUT if fault == "timeout" else None},
        {"kind": "llm", "name": "answer", "status": "error" if fault == "rate" else "ok",
         "error": RATE if fault == "rate" else None},
    ]
    return {
        "agent": agent, "version": version, "environment": environment, "status": "error" if failed else "ok",
        "error": next((s["error"] for s in steps if s.get("status") == "error"), None),
        "input": {"question": random.choice(QUESTIONS)},
        "output": None if failed else "See: Help article",
        "started_at": started.isoformat(), "ended_at": (started + timedelta(milliseconds=latency_ms)).isoformat(),
        "steps": steps,
    }


def draw(faults: list[tuple[str, float]]) -> str | None:
    roll = random.random()
    for name, p in faults:
        if roll < p:
            return name
        roll -= p
    return None


def show(report: dict) -> None:
    print(f"\n=== {report['candidate']['version']} ({report['candidate']['runs']} canary runs) vs "
          f"{report['baseline']['version']} ({report['baseline']['runs']} production runs) ===")
    print(f"verdict: {report['verdict'].upper()}")
    for reason in report["reasons"]:
        print(f"  - {reason}")
    for c in report["checks"]:
        print(f"  [{c['status']:<7}] {c['name']:<8} {c['reason']}")
    for w in report["warnings"]:
        print(f"  warning: {w}")


def main() -> None:
    random.seed(7)
    agent = f"canary-demo-{int(time.time())}"
    now = datetime.now(timezone.utc)
    plans = [
        ("v1", "production", 150, [("timeout", 0.03), ("rate", 0.02)], 400, 60),
        ("v2", "canary", 80, [("timeout", 0.30), ("rate", 0.02)], 900, 150),
        ("v3", "canary", 60, [("timeout", 0.03), ("rate", 0.02)], 410, 60),
    ]
    with httpx.Client(base_url=URL, timeout=60, headers=HEADERS) as http:
        for label in ("v1", "v2", "v3"):
            http.post(f"/v1/agents/{agent}/versions", json={"label": label, "prompt": f"demo {label}", "activate": False}).raise_for_status()
        for version, env, n, faults, mean_ms, sd_ms in plans:
            for i in range(n):
                started = now - timedelta(hours=3) + timedelta(seconds=i * 30)
                latency = max(50, int(random.gauss(mean_ms, sd_ms)))
                http.post("/v1/runs", json=make_run(agent, version, env, started, draw(faults), latency)).raise_for_status()
        print(f"sent runs for agent '{agent}': v1 production x150, v2 canary x80, v3 canary x60")

        for candidate in ("v2", "v3"):
            r = http.post(f"/v1/agents/{agent}/canary/evaluate", json={"baseline": "v1", "candidate": candidate})
            r.raise_for_status()
            show(r.json())

        history = http.get(f"/v1/agents/{agent}/canary").json()
        print(f"\n{len(history)} reports saved; browse them at {URL}/docs  (GET /v1/agents/{agent}/canary)")
        print("Each evaluation is also in the audit trail as 'canary.evaluate'.")


if __name__ == "__main__":
    main()
