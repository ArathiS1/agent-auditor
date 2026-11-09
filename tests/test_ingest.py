from fastapi.testclient import TestClient


RUN = {
    "agent": "support-bot",
    "input": {"question": "How do I reset my password?"},
    "output": {"answer": "Use the reset link."},
    "started_at": "2026-09-29T10:00:00Z",
    "ended_at": "2026-09-29T10:00:02Z",
    "steps": [
        {"kind": "retrieval", "name": "kb_search", "output": {"docs": ["a", "b"]},
         "attributes": {"scores": [0.82, 0.41]},
         "started_at": "2026-09-29T10:00:00Z", "ended_at": "2026-09-29T10:00:00.500Z"},
        {"kind": "llm", "name": "answer", "attributes": {"model": "example-model"}},
    ],
}


def test_health(client: TestClient):
    assert client.get("/v1/health").json() == {"status": "ok"}


def test_ingest_creates_agent_and_preserves_step_order(client: TestClient):
    r = client.post("/v1/runs", json=RUN)
    assert r.status_code == 201
    body = r.json()
    assert body["latency_ms"] == 2000.0
    assert [s["name"] for s in body["steps"]] == ["kb_search", "answer"]
    assert body["steps"][0]["latency_ms"] == 500.0
    assert body["step_count"] == 2

    assert [a["name"] for a in client.get("/v1/agents").json()] == ["support-bot"]
    fetched = client.get(f"/v1/runs/{body['id']}").json()
    assert fetched["steps"][0]["attributes"]["scores"] == [0.82, 0.41]


def test_list_runs_filters(client: TestClient):
    client.post("/v1/runs", json=RUN)
    client.post("/v1/runs", json={**RUN, "status": "error", "error": "timeout"})
    assert len(client.get("/v1/runs").json()) == 2
    errors = client.get("/v1/runs", params={"status": "error"}).json()
    assert len(errors) == 1 and errors[0]["error"] == "timeout"
    assert client.get("/v1/runs", params={"agent": "nobody"}).json() == []
    assert client.get("/v1/runs", params={"status": "bogus"}).status_code == 422


def test_ended_before_started_is_rejected(client: TestClient):
    bad = {**RUN, "started_at": "2026-09-29T10:00:05Z", "ended_at": "2026-09-29T10:00:00Z"}
    assert client.post("/v1/runs", json=bad).status_code == 422


def test_naive_timestamps_are_treated_as_utc(client: TestClient):
    r = client.post("/v1/runs", json={**RUN, "started_at": "2026-09-29T10:00:00", "ended_at": "2026-09-29T10:00:01Z"})
    assert r.status_code == 201 and r.json()["latency_ms"] == 1000.0


def test_get_missing_run_404(client: TestClient):
    assert client.get("/v1/runs/doesnotexist").status_code == 404
