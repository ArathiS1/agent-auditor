"""Deterministic synthetic runs for drift tests (plain Python, no server needed)."""
from __future__ import annotations

import math
import random

from agent_auditor.evals import RunView, StepView

BASE_TOPICS = [
    ("reset", "password"), ("change", "email"), ("cancel", "plan"),
    ("invoice", "copy"), ("update", "card"), ("delete", "account"),
]
NEW_TOPICS = [("refund", "payment"), ("chargeback", "dispute")]


def make_runs(
    n: int,
    rng: random.Random,
    *,
    latency: float = 900.0,
    score: float = 0.8,
    miss_rate: float = 0.04,
    error_rate: float = 0.03,
    topics=BASE_TOPICS,
    tools=("lookup_account", "send_email", "check_plan"),
) -> list[RunView]:
    runs = []
    for i in range(n):
        topic = rng.choice(topics)
        missed = rng.random() < miss_rate
        failed = rng.random() < error_rate
        docs = [] if missed else [f"Help article about {topic[0]} {topic[1]}"] * rng.choice((2, 3, 4))
        scores = [] if missed else [round(min(0.99, max(0.05, rng.gauss(score, 0.08))), 3)]
        steps = [
            StepView("retrieval", "kb_search", output=docs, attributes={"scores": scores}),
            StepView("tool", rng.choice(tools), status="error" if failed else "ok"),
            StepView("llm", "answer"),
        ]
        runs.append(RunView(
            id=f"r{i}", status="error" if failed else "ok",
            input=f"How do I {topic[0]} my {topic[1]}?",
            output="I could not find that." if missed else f"To {topic[0]} your {topic[1]}, open the help article.",
            latency_ms=latency * math.exp(rng.gauss(0, 0.15)), steps=tuple(steps),
        ))
    return runs


# ---- fixed-timestamp payloads for the API tests (also replayed through the pure detector) ----
from datetime import datetime, timedelta, timezone  # noqa: E402

from agent_auditor.evals import RunView as _RunView, StepView as _StepView  # noqa: E402

AS_OF = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
API_TOPICS = [("reset", "password"), ("change", "email"), ("cancel", "plan"), ("invoice", "copy")]
API_NEW_TOPICS = [("refund", "payment"), ("chargeback", "dispute")]
DRIFTED = dict(
    latency_cycle=(1800, 2000, 2200, 1900, 2000), topics=API_NEW_TOPICS, empty_every=3,
    tools=("lookup_account", "issue_refund"),
)


def window_payloads(
    agent, start, end, n, *, version="v1", environment="production",
    latency_cycle=(800, 900, 1000, 1100, 900), topics=API_TOPICS, empty_every=0, score=0.85, tools=("lookup_account",),
):
    """`n` run payloads spread evenly over [start, end), with deterministic, repeating values."""
    step = (end - start) / n
    out = []
    for i in range(n):
        t0 = start + step * i
        topic = topics[i % len(topics)]
        empty = bool(empty_every) and i % empty_every == 0
        out.append({
            "agent": agent, "version": version, "environment": environment, "status": "ok",
            "input": f"How do I {topic[0]} my {topic[1]}?",
            "output": "I could not find that." if empty else f"To {topic[0]} your {topic[1]}, open the help article.",
            "started_at": t0.isoformat(),
            "ended_at": (t0 + timedelta(milliseconds=latency_cycle[i % len(latency_cycle)])).isoformat(),
            "steps": [
                {"kind": "retrieval", "name": "kb_search", "output": [] if empty else ["Help article", "Account overview"],
                 "attributes": {"scores": [] if empty else [score]}},
                {"kind": "tool", "name": tools[i % len(tools)]},
                {"kind": "llm", "name": "answer"},
            ],
        })
    return out


def payload_to_runview(p) -> _RunView:
    started = datetime.fromisoformat(p["started_at"])
    ended = datetime.fromisoformat(p["ended_at"])
    return _RunView(
        id="x", status=p["status"], input=p["input"], output=p["output"],
        latency_ms=(ended - started).total_seconds() * 1000.0,
        steps=tuple(
            _StepView(s["kind"], s["name"], output=s.get("output"), attributes=s.get("attributes", {})) for s in p["steps"]
        ),
    )
