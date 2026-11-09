"""End-to-end tests for root-cause diagnosis (in-memory SQLite). Needs the full dev install."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

NOW = datetime.now(timezone.utc)
_n = [0]


def post_run(client: TestClient, question="How do I get a refund?", *, agent="bot", version="v1", docs=3,
             tool_error=None, llm_error=None, retrieval_error=None, run_error=None, age_minutes=None) -> dict:
    _n[0] += 1
    started = NOW - (timedelta(minutes=age_minutes) if age_minutes else timedelta(seconds=_n[0]))
    steps = [{"kind": "retrieval", "name": "kb_search", "output": ["article"] * docs,
              "attributes": {"num_docs": docs, "scores": [0.9] * min(docs, 1)}}]
    if retrieval_error:
        steps = [{"kind": "retrieval", "name": "kb_search", "status": "error", "error": retrieval_error}]
    steps.append({"kind": "tool", "name": "payments_api", "status": "error" if tool_error else "ok", "error": tool_error})
    steps.append({"kind": "llm", "name": "answer", "status": "error" if llm_error else "ok", "error": llm_error})
    failed = bool(tool_error or llm_error or retrieval_error or run_error)
    payload = {
        "agent": agent, "version": version, "environment": "production", "status": "error" if failed else "ok",
        "error": (tool_error or llm_error or retrieval_error or run_error) if failed else None,
        "input": {"question": question},
        "output": None if failed else ("Here is the answer from the article." if docs else "I could not find that."),
        "started_at": started.isoformat(), "ended_at": (started + timedelta(milliseconds=300)).isoformat(),
        "steps": steps, "tags": {},
    }
    r = client.post("/v1/runs", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def register(client: TestClient, *labels: str) -> None:
    for label in labels:
        r = client.post("/v1/agents/bot/versions", json={"label": label, "prompt": label, "activate": False})
        assert r.status_code in (200, 201), r.text


TIMEOUT = "TimeoutError: payments api timed out"


def seed_mixed(client: TestClient) -> None:
    register(client, "v1")
    for _ in range(3):
        post_run(client)
    post_run(client, tool_error=TIMEOUT)
    post_run(client, tool_error=TIMEOUT)
    post_run(client, docs=0)
    post_run(client, llm_error="openai.RateLimitError: Error code: 429")
    post_run(client, run_error="ValueError: could not parse model output as JSON")


# ---------------------------------------------------------------- diagnose
def test_batch_diagnosis_counts_causes_and_is_idempotent(client: TestClient):
    seed_mixed(client)
    r = client.post("/v1/agents/bot/diagnose", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["classified"], out["failed"], out["healthy"]) == (8, 5, 3)
    assert out["by_cause"] == {"tool_timeout": 2, "retrieval_empty": 1, "llm_error": 1, "agent_exception": 1}
    assert client.post("/v1/agents/bot/diagnose", json={}).json()["classified"] == 0  # nothing new
    assert client.post("/v1/agents/bot/diagnose", json={"reclassify": True}).json()["classified"] == 8
    report = client.post("/v1/agents/bot/root-causes", json={"diagnose_first": False}).json()
    assert report["total_runs"] == 8  # re-classifying updated rows, it did not duplicate them


def test_unknown_agent_and_version_are_404(client: TestClient):
    assert client.post("/v1/agents/ghost/diagnose", json={}).status_code == 404
    seed_mixed(client)
    assert client.post("/v1/agents/bot/diagnose", json={"version": "nope"}).status_code == 404
    assert client.post("/v1/agents/ghost/root-causes", json={}).status_code == 404


def test_single_run_diagnosis_has_evidence_and_the_run_is_stored(client: TestClient):
    register(client, "v1")
    bad = post_run(client, tool_error=TIMEOUT)
    d = client.get(f"/v1/runs/{bad['id']}/diagnosis")
    assert d.status_code == 200, d.text
    body = d.json()
    assert (body["status"], body["cause"], body["strength"], body["subcause"], body["step"]) == (
        "failed", "tool_timeout", "strong", "timeout", "payments_api")
    assert body["version"] == "v1" and "timed out" in body["explanation"] and body["findings"][0]["cause"] == "tool_timeout"
    assert "run_succeeded" in body["symptoms"] and "no_step_errors" in body["symptoms"]
    good = client.get(f"/v1/runs/{post_run(client)['id']}/diagnosis").json()
    assert good["status"] == "healthy" and good["cause"] is None
    assert client.get("/v1/runs/nope/diagnosis").status_code == 404
    assert client.post("/v1/agents/bot/root-causes", json={"diagnose_first": False}).json()["total_runs"] == 2


# ---------------------------------------------------------------- report
def seed_two_versions(client: TestClient) -> None:
    register(client, "v1", "v2")
    for i in range(120):                                  # v1: 3% tool timeouts
        post_run(client, version="v1", tool_error=TIMEOUT if i < 4 else None)
    for i in range(80):                                   # v2: 30% empty retrieval
        post_run(client, version="v2", docs=0 if i < 24 else 3)


def test_report_ranks_causes_and_finds_the_version_that_introduced_one(client: TestClient):
    seed_two_versions(client)
    r = client.post("/v1/agents/bot/root-causes", json={})
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["diagnosed_now"] == 200 and report["total_runs"] == 200 and report["failed_runs"] == 28
    assert [c["cause"] for c in report["causes"]] == ["retrieval_empty", "tool_timeout"]
    top = report["causes"][0]
    assert top["count"] == 24 and top["top_steps"] == [{"name": "kb_search", "count": 24}] and top["advice"] and len(top["example_run_ids"]) == 3
    timeout = report["causes"][1]
    assert timeout["top_steps"][0]["name"] == "payments_api" and timeout["subcauses"][0]["name"] == "timeout"
    assoc = report["version_associations"]["associations"]
    assert [(a["cause"], a["version"], a["direction"]) for a in assoc] == [("retrieval_empty", "v2", "more_common")]
    assert assoc[0]["rate_in_version"] == 0.3 and assoc[0]["p_value"] < 1e-6
    assert "not proof" in report["version_associations"]["note"]


def test_report_filters_by_version_and_time(client: TestClient):
    seed_two_versions(client)
    for _ in range(20):
        post_run(client, version="v1", tool_error=TIMEOUT, age_minutes=300)   # old runs, outside a 2 hour window
    only_v2 = client.post("/v1/agents/bot/root-causes", json={"version": "v2"}).json()
    assert only_v2["total_runs"] == 80 and [c["cause"] for c in only_v2["causes"]] == ["retrieval_empty"]
    assert only_v2["version_associations"]["associations"] == []
    recent = client.post("/v1/agents/bot/root-causes", json={"since_hours": 2}).json()
    assert recent["total_runs"] == 200 and recent["failed_runs"] == 28    # the 20 old failures are excluded
    assert client.post("/v1/agents/bot/root-causes", json={}).json()["total_runs"] == 220
    empty = client.post("/v1/agents/bot/root-causes", json={"since_hours": 0.0001}).json()
    assert empty["total_runs"] == 0 and any("no diagnosed runs" in n for n in empty["notes"])


def test_taxonomy_lists_every_cause_with_advice(client: TestClient):
    r = client.get("/v1/root-causes/taxonomy")
    assert r.status_code == 200
    causes = {c["cause"]: c for c in r.json()}
    assert {"tool_timeout", "retrieval_empty", "llm_error", "agent_exception", "unknown"} <= set(causes)
    assert all(c["meaning"] and c["advice"] for c in causes.values())


# ---------------------------------------------------------------- Phase 5 link
def test_generated_regression_cases_carry_their_root_cause(client: TestClient):
    register(client, "v1")
    post_run(client, "Where is my refund?", docs=0)
    post_run(client, "Charge my card again", tool_error=TIMEOUT)
    r = client.post("/v1/agents/bot/tests/generate", json={})
    assert r.status_code == 200, r.text
    causes = {c["input"]["question"]: c["failure"]["cause"] for c in r.json()["created"]}
    assert causes == {"Where is my refund?": "retrieval_empty", "Charge my card again": "tool_timeout"}
