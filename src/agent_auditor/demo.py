"""Sample data for `agent-auditor demo`: one support bot, a messy week, and a gated release.

The data is made up. It is shaped like a real incident so every dashboard page has something to show:
tool timeouts that begin in the last few hours, a burst of rate limits, empty answers after a bad
search, slow runs, a clean candidate (v2) that is released and later rolled back, and a bad
candidate (v3) that the gate refuses.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import httpx

AGENT = "support-bot"
FAQ = {
    "How do I reset my password?": "To reset your password open Settings, choose Account, then select Reset password. A reset link is emailed to you.",
    "Cancel my subscription": "To cancel your subscription open Billing, choose Manage plan, then select Cancel plan. Access continues until the end of the billing period.",
    "Where is my invoice for March?": "Invoices are listed under Billing, then History. A PDF copy of each invoice is also emailed to the billing contact every month.",
    "Can I change my billing email?": "You can change the billing email under Billing, then Contacts. The new address receives the next invoice.",
    "Why was my card charged twice?": "A second charge is usually a pending authorisation that disappears within five days. If it stays, contact support with the invoice number.",
    "How do I export my data?": "To export your data open Settings, choose Privacy, then select Export data. The export is a zip file emailed within one hour.",
    "Do you offer student pricing?": "Students get a discount of fifty percent on the Pro plan. Verify a student email under Billing, then Discounts.",
    "My login code never arrives": "If the login code does not arrive check your spam folder, then request a new code. Codes expire after ten minutes.",
    "Upgrade me to the team plan": "To upgrade to the team plan open Billing, choose Manage plan, then select Team. You are charged the difference for the current month.",
    "How do I delete my account?": "To delete your account open Settings, choose Privacy, then select Delete account. Deletion is permanent after thirty days.",
    "What payment methods do you accept?": "We accept major credit cards, debit cards and bank transfer for annual plans. Add a method under Billing, then Payment methods.",
    "Is there an API rate limit?": "The API allows one hundred requests per minute on the Pro plan. Higher limits are available on the Team plan.",
}
QUESTIONS = list(FAQ)
TIMEOUT = "httpx.ReadTimeout: The read operation timed out"
RATE_LIMIT = "429 Too Many Requests: rate limit exceeded, retry after 20s"


def _run(version: str, env: str, started: datetime, rng: random.Random, kinds: list[tuple[str, float]]) -> dict:
    """Build one run. `kinds` is a list of (failure kind, probability); no match means a healthy run."""
    roll, kind, total = rng.random(), "ok", 0.0
    for name, p in kinds:
        total += p
        if roll < total:
            kind = name
            break
    question = rng.choice(QUESTIONS)
    article = FAQ[question]
    latency = int(max(500, rng.gauss(1400, 350)))
    status, error, output = "ok", None, article
    docs = 3
    steps_tool = {"status": "ok", "error": None}
    if kind == "tool_timeout":
        status, error, output, latency = "error", TIMEOUT, None, int(rng.uniform(9000, 12000))
        steps_tool = {"status": "error", "error": TIMEOUT}
    elif kind == "rate_limit":
        status, error, output, latency = "error", RATE_LIMIT, None, int(rng.uniform(300, 900))
    elif kind == "empty_search":
        docs, output = 0, ""
    elif kind == "slow":
        latency = int(rng.uniform(11000, 19000))
    steps = [
        {"kind": "retrieval", "name": "kb_search", "input": question, "output": [article] * docs,
         "attributes": {"num_docs": docs, "scores": [round(rng.uniform(0.6, 0.9), 2)] * docs}},
        {"kind": "tool", "name": "payments_api", **steps_tool},
        {"kind": "llm", "name": "answer", "status": "error" if kind == "rate_limit" else "ok",
         "error": RATE_LIMIT if kind == "rate_limit" else None},
    ]
    return {
        "agent": AGENT, "version": version, "environment": env, "status": status, "error": error,
        "input": {"question": question}, "output": output, "steps": steps,
        "started_at": started.isoformat(), "ended_at": (started + timedelta(milliseconds=latency)).isoformat(),
    }


def seed(http: httpx.Client, say=print) -> str:
    """Fill a running Auditor (reached through `http`) with the demo scenario. Returns the agent name."""
    rng = random.Random(7)
    now = datetime.now(timezone.utc)

    def call(method: str, path: str, **kw):
        r = http.request(method, path, **kw)
        r.raise_for_status()
        return r.json()

    call("POST", "/v1/agents", json={"name": AGENT, "description": "Customer support assistant (sample data)"})
    versions = [("v1", "You are a support assistant. Answer from the knowledge base.", True),
                ("v2", "You are a support assistant. Answer from the knowledge base and cite the article.", False),
                ("v3", "Answer briefly. Skip the knowledge base when you are confident.", False)]
    for label, prompt, active in versions:
        call("POST", f"/v1/agents/{AGENT}/versions", json={"label": label, "prompt": prompt, "config": {"top_k": 4}, "activate": active})

    say("  adding 240 sample runs ...")
    calm = [("tool_timeout", 0.02), ("rate_limit", 0.01), ("empty_search", 0.03), ("slow", 0.03)]
    rough = [("tool_timeout", 0.07), ("rate_limit", 0.04), ("empty_search", 0.05), ("slow", 0.05)]
    bad = [("tool_timeout", 0.32), ("empty_search", 0.2), ("slow", 0.15)]
    for i in range(130):  # older production traffic, mostly healthy
        started = now - timedelta(hours=46) + timedelta(minutes=i * 12 + rng.randint(0, 5))
        call("POST", "/v1/runs", json=_run("v1", "production", started, rng, calm))
    for i in range(70):  # the last ~14 hours: things get worse
        started = now - timedelta(hours=14) + timedelta(minutes=i * 12 + rng.randint(0, 5))
        call("POST", "/v1/runs", json=_run("v1", "production", started, rng, rough))
    for version, n, kinds in (("v2", 40, calm), ("v3", 40, bad)):
        for i in range(n):
            started = now - timedelta(hours=5) + timedelta(minutes=i * 6)
            call("POST", "/v1/runs", json=_run(version, "canary", started, rng, kinds))

    say("  scoring, diagnosing, writing regression tests ...")
    call("POST", "/v1/evals/run", json={"agent": AGENT, "limit": 500})
    call("POST", f"/v1/agents/{AGENT}/diagnose", json={"limit": 500})
    call("POST", f"/v1/agents/{AGENT}/tests/generate", json={})
    try:
        call("POST", "/v1/drift/check", json={"agent": AGENT, "current_hours": 12, "reference_hours": 36, "min_samples": 20})
    except httpx.HTTPError:
        pass  # drift needs enough data in both windows; the rest of the demo does not depend on it

    say("  canary checks and a gated release ...")
    reports = {}
    for cand in ("v2", "v3"):
        reports[cand] = call("POST", f"/v1/agents/{AGENT}/canary/evaluate", json={"baseline": "v1", "candidate": cand})
    rel = call("POST", f"/v1/agents/{AGENT}/releases", json={"candidate": "v2", "proposed_by": "alice", "notes": "cite the article in answers"})
    rid = rel["id"]
    for method, path, body in (
        ("POST", f"/v1/releases/{rid}/promote", {"actor": "alice"}),       # refused: no approval yet
        ("POST", f"/v1/releases/{rid}/approve", {"approver": "alice"}),    # refused: cannot approve your own
        ("POST", f"/v1/releases/{rid}/approve", {"approver": "bob", "comment": "canary looks clean"}),
        ("POST", f"/v1/releases/{rid}/promote", {"actor": "alice"}),
        ("POST", f"/v1/releases/{rid}/rollback", {"actor": "bob", "reason": "payments timeouts doubled after the release"}),
    ):
        http.request(method, path, json=body)  # the first two are meant to be refused, so no raise_for_status
    bad = call("POST", f"/v1/agents/{AGENT}/releases", json={"candidate": "v3", "proposed_by": "alice", "notes": "shorter answers"})
    http.request("POST", f"/v1/releases/{bad['id']}/approve", json={"approver": "bob"})
    http.request("POST", f"/v1/releases/{bad['id']}/promote", json={"actor": "alice"})
    say("  done.")
    return AGENT
