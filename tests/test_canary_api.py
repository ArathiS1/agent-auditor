"""End-to-end tests for canary evaluation (in-memory SQLite). Needs the full dev install."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_auditor.models import Agent, AuditEvent, ReplayRun

NOW = datetime.now(timezone.utc)
_n = [0]
TIMEOUT = "TimeoutError: payments api timed out"


def post_run(client: TestClient, *, version: str, environment: str, fail: bool = False, latency_ms: int | None = None) -> None:
    _n[0] += 1
    started = NOW - timedelta(seconds=_n[0])
    latency = latency_ms if latency_ms is not None else 250 + (_n[0] % 7) * 20
    steps = [
        {"kind": "retrieval", "name": "kb_search", "output": ["article"] * 3, "attributes": {"num_docs": 3, "scores": [0.9]}},
        {"kind": "tool", "name": "payments_api", "status": "error" if fail else "ok", "error": TIMEOUT if fail else None},
        {"kind": "llm", "name": "answer"},
    ]
    payload = {
        "agent": "bot", "version": version, "environment": environment, "status": "error" if fail else "ok",
        "error": TIMEOUT if fail else None, "input": {"question": f"question {_n[0] % 5}"},
        "output": None if fail else "Here is the answer from the article.",
        "started_at": started.isoformat(), "ended_at": (started + timedelta(milliseconds=latency)).isoformat(),
        "steps": steps, "tags": {},
    }
    r = client.post("/v1/runs", json=payload)
    assert r.status_code == 201, r.text


def register(client: TestClient, *labels: str) -> None:
    for label in labels:
        r = client.post("/v1/agents/bot/versions", json={"label": label, "prompt": label, "activate": False})
        assert r.status_code in (200, 201), r.text


def seed(client: TestClient, *, baseline=60, canary=40, canary_failures=0, canary_latency=None) -> None:
    register(client, "v1", "v2")
    for _ in range(baseline):
        post_run(client, version="v1", environment="production")
    for i in range(canary):
        post_run(client, version="v2", environment="canary", fail=i < canary_failures, latency_ms=canary_latency)


def evaluate(client: TestClient, **body):
    return client.post("/v1/agents/bot/canary/evaluate", json={"baseline": "v1", "candidate": "v2", **body})


def check(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["name"] == name)


def test_healthy_canary_is_promoted(client):
    seed(client)
    r = evaluate(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict"] == "promote", body["reasons"]
    assert body["candidate"]["runs"] == 40 and body["baseline"]["runs"] == 60
    assert check(body, "evals")["status"] == "pass" and check(body, "replay")["status"] == "skipped"


def test_failing_canary_is_rolled_back(client):
    seed(client, canary_failures=16)
    body = evaluate(client).json()
    assert body["verdict"] == "rollback"
    assert "evals" in body["blocking"] and "causes" in body["blocking"]
    assert any(c["cause"] == "tool_timeout" and c["new"] for c in check(body, "causes")["details"]["causes"])


def test_slow_canary_is_held_or_rolled_back(client):
    seed(client, canary_latency=900)
    body = evaluate(client).json()
    assert body["verdict"] in ("hold", "rollback") and "latency" in body["blocking"]


def test_thin_traffic_means_continue(client):
    seed(client, baseline=60, canary=5)
    body = evaluate(client).json()
    assert body["verdict"] == "continue"
    assert any("canary runs" in reason for reason in body["reasons"])


def test_thin_traffic_but_failed_replay_still_rolls_back(client):
    seed(client, baseline=60, canary=5)
    factory = client.app.state.session_factory
    with factory() as db:
        agent = db.scalar(select(Agent).where(Agent.name == "bot"))
        db.add(ReplayRun(agent_id=agent.id, version_label="v2", verdict="fail",
                         summary={"verdict": "fail", "total": 10, "failed": 3, "fixed": [],
                                  "still_failing": [], "newly_failing": ["c1", "c2", "c3"]}))
        db.commit()
    body = evaluate(client).json()
    assert body["verdict"] == "rollback" and body["blocking"] == ["replay"]


def test_require_replay_holds_when_none_exists(client):
    seed(client)
    body = evaluate(client, policy={"require_replay": True}).json()
    assert body["verdict"] == "hold" and body["blocking"] == ["replay"]


def test_reports_are_saved_listed_and_audited(client):
    seed(client)
    first = evaluate(client).json()
    assert first["id"]
    assert client.get(f"/v1/canary/{first['id']}").json()["verdict"] == "promote"
    listing = client.get("/v1/agents/bot/canary").json()
    assert [r["id"] for r in listing] == [first["id"]]
    assert client.get("/v1/agents/bot/canary", params={"candidate": "nope"}).json() == []
    with client.app.state.session_factory() as db:
        events = db.scalars(select(AuditEvent).where(AuditEvent.action == "canary.evaluate")).all()
    assert len(events) == 1 and events[0].detail["verdict"] == "promote"


def test_save_false_stores_nothing(client):
    seed(client)
    body = evaluate(client, save=False).json()
    assert body["id"] is None
    assert client.get("/v1/agents/bot/canary").json() == []


def test_errors(client):
    seed(client)
    assert client.post("/v1/agents/ghost/canary/evaluate", json={"baseline": "v1", "candidate": "v2"}).status_code == 404
    assert evaluate(client, candidate="v9").status_code == 404
    assert client.get("/v1/canary/nope").status_code == 404
    same = client.post("/v1/agents/bot/canary/evaluate", json={
        "baseline": "v1", "candidate": "v1", "candidate_environment": "production"})
    assert same.status_code == 422
    assert evaluate(client, policy={"min_effect": 0.5, "rollback_effect": 0.1}).status_code == 422
    assert evaluate(client, since_hours=0).status_code == 422


def test_a_regression_is_attributed_to_the_kind_of_change(client):
    seed(client, canary_failures=16)  # v1 and v2 differ only in their prompt text
    body = evaluate(client).json()
    change = check(body, "change")
    assert change["status"] == "pass" and change["details"]["changed"] == ["prompt"]
    assert change["details"]["attribution"][0]["cause"] == "prompt_regression"
    assert "prompt regression" in change["reason"]
    assert body["verdict"] == "rollback"  # the attribution informs, it never changes the decision


def test_no_attribution_when_nothing_regressed(client):
    seed(client)
    assert check(evaluate(client).json(), "change")["status"] == "skipped"
