"""Phase 8 demo: a gated release with a human approval, a rollback, and a blocked bad candidate.

v1 is active. v2 canaries cleanly and is released with an approval, then rolled back by a person.
v3 canaries badly: the gate refuses to approve or promote it.

1) Start the server:   uvicorn agent_auditor.api.main:app --reload
2) Run this script:    python examples/release_demo.py
"""
import os
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

URL = os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000")
HEADERS = {"X-API-Key": os.environ["AUDITOR_API_KEY"]} if os.environ.get("AUDITOR_API_KEY") else {}
TIMEOUT = "httpx.ReadTimeout: The read operation timed out"


def make_run(agent: str, version: str, environment: str, started: datetime, fail: bool, latency_ms: int) -> dict:
    steps = [
        {"kind": "retrieval", "name": "kb_search", "output": ["Help article"] * 3, "attributes": {"num_docs": 3, "scores": [0.82]}},
        {"kind": "tool", "name": "payments_api", "status": "error" if fail else "ok", "error": TIMEOUT if fail else None},
        {"kind": "llm", "name": "answer"},
    ]
    return {
        "agent": agent, "version": version, "environment": environment, "status": "error" if fail else "ok",
        "error": TIMEOUT if fail else None, "input": {"question": random.choice(["reset password", "cancel plan", "invoice"])},
        "output": None if fail else "See: Help article",
        "started_at": started.isoformat(), "ended_at": (started + timedelta(milliseconds=latency_ms)).isoformat(),
        "steps": steps,
    }


def step(title: str) -> None:
    print(f"\n--- {title}")


def show(r: httpx.Response, ok: tuple[int, ...] = (200, 201)) -> dict:
    body = r.json()
    detail = body.get("detail", body) if r.status_code not in ok else body
    status = "OK     " if r.status_code in ok else f"REFUSED {r.status_code}"
    if r.status_code in ok:
        print(f"  [{status}] state={body.get('state')}  {body.get('recommendation', '')}")
    else:
        print(f"  [{status}] {detail}")
    return body


def main() -> None:
    random.seed(11)
    agent = f"release-demo-{int(time.time())}"
    now = datetime.now(timezone.utc)
    with httpx.Client(base_url=URL, timeout=60, headers=HEADERS) as http:
        for label, activate in (("v1", True), ("v2", False), ("v3", False)):
            http.post(f"/v1/agents/{agent}/versions", json={"label": label, "prompt": f"demo {label}", "activate": activate}).raise_for_status()
        for version, env, n, fail_rate in (("v1", "production", 100, 0.03), ("v2", "canary", 50, 0.03), ("v3", "canary", 50, 0.35)):
            for i in range(n):
                started = now - timedelta(hours=2) + timedelta(seconds=i * 20)
                http.post("/v1/runs", json=make_run(agent, version, env, started, random.random() < fail_rate, int(random.gauss(400, 50)))).raise_for_status()

        def active() -> str:
            versions = http.get(f"/v1/agents/{agent}/versions").json()
            return next((v["label"] for v in versions if v["is_active"]), "none")

        def canary(candidate: str) -> str:
            report = http.post(f"/v1/agents/{agent}/canary/evaluate", json={"baseline": "v1", "candidate": candidate}).json()
            print(f"  canary {candidate} vs v1: {report['verdict'].upper()}  {'; '.join(report['reasons'])[:110]}")
            return report["verdict"]

        print(f"agent '{agent}'; active version: {active()}")

        step("v2: canary evidence, then propose a release")
        canary("v2")
        rel = show(http.post(f"/v1/agents/{agent}/releases", json={"candidate": "v2", "proposed_by": "alice", "notes": "new retrieval prompt"}))
        rid = rel["id"]

        step("Promote with no approval yet")
        show(http.post(f"/v1/releases/{rid}/promote", json={"actor": "alice"}))
        step("Alice approves her own release")
        show(http.post(f"/v1/releases/{rid}/approve", json={"approver": "alice"}))
        step("Bob approves, then Alice promotes")
        show(http.post(f"/v1/releases/{rid}/approve", json={"approver": "bob", "comment": "canary looks clean"}))
        show(http.post(f"/v1/releases/{rid}/promote", json={"actor": "alice"}))
        print(f"  active version is now: {active()}")

        step("Later: errors rise in production, Bob rolls it back")
        show(http.post(f"/v1/releases/{rid}/rollback", json={"actor": "bob", "reason": "payments timeouts doubled after the release"}))
        print(f"  active version is now: {active()}")

        step("v3: a bad canary cannot be approved or promoted")
        canary("v3")
        bad = show(http.post(f"/v1/agents/{agent}/releases", json={"candidate": "v3", "proposed_by": "alice"}))
        show(http.post(f"/v1/releases/{bad['id']}/approve", json={"approver": "bob"}))
        show(http.post(f"/v1/releases/{bad['id']}/promote", json={"actor": "alice"}))
        print(f"  active version is still: {active()}")

        step(f"Audit trail for the v2 release ({rid[:8]})")
        for e in http.get(f"/v1/releases/{rid}/audit").json():
            print(f"  {e['created_at'][11:19]}  {e['actor']:<6} {e['action']}")
        print(f"\nBrowse: {URL}/docs  (GET /v1/audit?action_prefix=release.)")


if __name__ == "__main__":
    main()
