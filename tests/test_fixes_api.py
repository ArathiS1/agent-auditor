"""End-to-end tests for fix proposals and suspect-source detection (in-memory SQLite). Needs the full dev install."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

NOW = datetime.now(timezone.utc)
_n = [0]


def post_run(client: TestClient, *, docs=("faq.md",), fail=False, empty=False, agent="bot", version="v1") -> None:
    _n[0] += 1
    started = NOW - timedelta(seconds=_n[0])
    retrieved = [{"source": d, "text": "article"} for d in docs]
    steps = [{"kind": "retrieval", "name": "kb_search", "output": retrieved,
              "attributes": {"num_docs": len(retrieved), "scores": [0.9] * min(len(retrieved), 1)}},
             {"kind": "llm", "name": "answer"}]
    payload = {"agent": agent, "version": version, "environment": "production", "status": "ok",
               "input": {"question": f"question {_n[0]}"}, "output": "" if empty else "Here is the answer from the article.",
               "started_at": started.isoformat(), "ended_at": (started + timedelta(milliseconds=200)).isoformat(),
               "steps": steps, "tags": {}}
    if fail:  # no documents found, so the diagnosis is retrieval_empty
        payload["steps"][0].update(output=[], attributes={"num_docs": 0, "scores": []})
        payload["output"] = "I could not find that."
    assert client.post("/v1/runs", json=payload).status_code == 201


def register(client: TestClient, label="v1", *, activate=True, config=None) -> None:
    r = client.post("/v1/agents/bot/versions", json={"label": label, "prompt": "Be helpful.", "activate": activate,
                                                      "config": config if config is not None else {"top_k": 3, "min_score": 0.3}})
    assert r.status_code in (200, 201), r.text


def test_proposals_target_the_measured_cause_and_change_one_thing_each(client: TestClient):
    register(client)
    for _ in range(6):
        post_run(client)
    for _ in range(4):
        post_run(client, fail=True)
    r = client.post("/v1/agents/bot/fixes/propose", json={})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["base_version"]["label"] == "v1" and out["failed_runs"] == 4
    fields = [c["changes"][0]["field"] for c in out["candidates"] if c["verifiable"]]
    assert fields[:2] == ["top_k", "min_score"]
    top_k = out["candidates"][0]
    assert top_k["config"] == {"top_k": 5, "min_score": 0.3} and top_k["cause"] == "retrieval_empty"
    assert [v["label"] for v in client.get("/v1/agents/bot/versions").json() if v["is_active"]] == ["v1"]  # nothing was changed


def test_no_active_version_or_unknown_agent_is_a_clear_error(client: TestClient):
    assert client.post("/v1/agents/ghost/fixes/propose", json={}).status_code == 404
    register(client, activate=False)
    r = client.post("/v1/agents/bot/fixes/propose", json={})
    assert r.status_code == 422 and "no active version" in r.json()["detail"]
    assert client.post("/v1/agents/bot/fixes/propose", json={"base_version": "nope"}).status_code == 404


def test_no_failures_means_no_candidates_and_says_so(client: TestClient):
    register(client)
    for _ in range(5):
        post_run(client)
    out = client.post("/v1/agents/bot/fixes/propose", json={}).json()
    assert out["candidates"] == [] and any("nothing to fix" in n for n in out["notes"])


def test_ai_rewrite_without_a_key_is_skipped_not_an_error(client: TestClient):
    register(client)
    for _ in range(4):
        post_run(client, fail=True)
    out = client.post("/v1/agents/bot/fixes/propose", json={"use_ai": True}).json()
    assert any("ANTHROPIC_API_KEY" in n for n in out["notes"]) and out["candidates"]


def test_a_document_that_keeps_showing_up_in_failures_is_reported(client: TestClient):
    register(client)
    for _ in range(40):
        post_run(client, docs=("faq.md", "guide.md"))
    for _ in range(12):
        post_run(client, docs=("old_policy.md", "faq.md"), empty=True)  # an empty answer is a failed run
    report = client.post("/v1/agents/bot/root-causes", json={}).json()
    assert [s["source"] for s in report["suspect_sources"]] == ["old_policy.md"]
    out = client.post("/v1/agents/bot/fixes/propose", json={}).json()
    manual = [c for c in out["candidates"] if c["cause"] == "bad_source_data"]
    assert manual and not manual[0]["verifiable"] and "old_policy.md" in manual[0]["title"]
