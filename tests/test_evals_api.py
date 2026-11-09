"""End-to-end tests for the eval endpoints (in-memory SQLite). Needs the full dev install."""
from fastapi.testclient import TestClient


def make_version(client: TestClient, agent: str, label: str) -> None:
    r = client.post(f"/v1/agents/{agent}/versions", json={"label": label, "prompt": label, "activate": False})
    assert r.status_code in (200, 201), r.text


def post_run(client: TestClient, agent="bot", version="v1", *, docs=2, status="ok", environment="production", output="Use the reset link", ended=True):
    body = {
        "agent": agent, "version": version, "environment": environment, "status": status, "output": output,
        "started_at": "2026-09-29T10:00:00Z",
        "steps": [{
            "kind": "retrieval", "name": "kb", "output": [f"doc{i}" for i in range(docs)],
            "attributes": {"scores": [0.9] if docs else []},
        }],
    }
    if ended:
        body["ended_at"] = "2026-09-29T10:00:01Z"
    r = client.post("/v1/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def seed(client, version, good, bad, **kw):
    for _ in range(good):
        post_run(client, version=version, docs=2, **kw)
    for _ in range(bad):
        post_run(client, version=version, docs=0, **kw)


def test_scorer_catalog(client: TestClient):
    names = {s["name"]: s for s in client.get("/v1/evals/scorers").json()}
    assert {"run_succeeded", "retrieval_nonempty", "grounded_overlap"} <= set(names)
    assert names["retrieval_top_score"]["defaults"] == {"threshold": 0.5}


def test_evaluate_stores_results_and_is_idempotent(client: TestClient):
    make_version(client, "bot", "v1")
    run_id = post_run(client, docs=0)
    first = client.post("/v1/evals/run", json={"agent": "bot"}).json()
    assert first["evaluated_runs"] == 1 and first["results_written"] >= 5 and first["scorer_errors"] == {}
    second = client.post("/v1/evals/run", json={"agent": "bot"}).json()
    assert second["evaluated_runs"] == 0 and second["results_written"] == 0

    results = {r["scorer"]: r for r in client.get(f"/v1/runs/{run_id}/evals").json()}
    assert results["retrieval_nonempty"]["applicable"] and results["retrieval_nonempty"]["passed"] is False
    assert results["run_succeeded"]["passed"] is True
    assert results["retrieval_top_score"]["applicable"] is False  # no scores -> not applicable
    assert results["retrieval_top_score"]["passed"] is None


def test_rescoring_updates_rows_instead_of_duplicating(client: TestClient):
    make_version(client, "bot", "v1")
    run_id = post_run(client)
    client.post("/v1/evals/run", json={"agent": "bot"})
    before = len(client.get(f"/v1/runs/{run_id}/evals").json())
    again = client.post("/v1/evals/run", json={"agent": "bot", "only_unevaluated": False}).json()
    assert again["evaluated_runs"] == 1
    assert len(client.get(f"/v1/runs/{run_id}/evals").json()) == before


def test_batches_walk_through_all_runs(client: TestClient):
    make_version(client, "bot", "v1")
    for _ in range(5):
        post_run(client)
    seen = 0
    for _ in range(4):
        out = client.post("/v1/evals/run", json={"agent": "bot", "limit": 2}).json()
        seen += out["evaluated_runs"]
        if out["evaluated_runs"] == 0:
            break
    assert seen == 5


def test_only_requested_scorers_run_and_new_scorer_fills_in_later(client: TestClient):
    make_version(client, "bot", "v1")
    run_id = post_run(client)
    client.post("/v1/evals/run", json={"agent": "bot", "scorers": ["run_succeeded"]})
    assert [r["scorer"] for r in client.get(f"/v1/runs/{run_id}/evals").json()] == ["run_succeeded"]
    client.post("/v1/evals/run", json={"agent": "bot", "scorers": ["run_succeeded", "output_present"]})
    assert {r["scorer"] for r in client.get(f"/v1/runs/{run_id}/evals").json()} == {"run_succeeded", "output_present"}


def test_bad_scorer_params_are_reported_not_fatal(client: TestClient):
    make_version(client, "bot", "v1")
    post_run(client)
    out = client.post("/v1/evals/run", json={
        "agent": "bot", "scorers": ["latency_within_budget", "run_succeeded"],
        "params": {"latency_within_budget": {"budget_ms": "fast"}},
    }).json()
    assert "budget_ms" in out["scorer_errors"]["latency_within_budget"] and out["scored"] >= 1


def test_request_validation(client: TestClient):
    make_version(client, "bot", "v1")
    assert client.post("/v1/evals/run", json={"scorers": ["nope"]}).status_code == 422
    assert client.post("/v1/evals/run", json={"scorers": []}).status_code == 422
    assert client.post("/v1/evals/run", json={"agent": "ghost"}).status_code == 404
    assert client.post("/v1/evals/run", json={"agent": "bot", "version": "zzz"}).status_code == 404
    assert client.post("/v1/evals/run", json={"version": "v1"}).status_code == 422
    assert client.post("/v1/evals/run", json={"limit": 0}).status_code == 422
    assert client.get("/v1/runs/missing/evals").status_code == 404


def test_summary_per_version(client: TestClient):
    for v in ("v1", "v2"):
        make_version(client, "bot", v)
    seed(client, "v1", good=8, bad=2)
    seed(client, "v2", good=5, bad=5)
    client.post("/v1/evals/run", json={"agent": "bot"})
    summary = client.get("/v1/agents/bot/eval-summary").json()
    by_version = {v["version"]: v for v in summary["versions"]}
    assert by_version["v1"]["runs"] == 10 and by_version["v1"]["evaluated_runs"] == 10
    stat = {s["scorer"]: s for s in by_version["v2"]["scorers"]}["retrieval_nonempty"]
    assert stat["n"] == 10 and stat["passed"] == 5 and stat["pass_rate"] == 0.5
    assert stat["ci_low"] < 0.5 < stat["ci_high"]
    assert client.get("/v1/agents/ghost/eval-summary").status_code == 404


def test_compare_flags_a_real_regression(client: TestClient):
    for v in ("v1", "v2"):
        make_version(client, "bot", v)
    seed(client, "v1", good=40, bad=0)
    seed(client, "v2", good=20, bad=20)
    client.post("/v1/evals/run", json={"agent": "bot", "limit": 500})
    cmp = client.get("/v1/agents/bot/compare", params={"baseline": "v1", "candidate": "v2"}).json()
    assert cmp["overall_verdict"] == "regression"
    row = {r["scorer"]: r for r in cmp["scorers"]}["retrieval_nonempty"]
    assert row["verdict"] == "regression" and row["delta"] == -0.5 and row["p_value"] < 0.001
    assert {r["scorer"]: r for r in cmp["scorers"]}["run_succeeded"]["verdict"] == "no_change"


def test_compare_no_change_and_insufficient_data(client: TestClient):
    for v in ("v1", "v2", "v3"):
        make_version(client, "bot", v)
    seed(client, "v1", good=30, bad=0)
    seed(client, "v2", good=30, bad=0)
    seed(client, "v3", good=3, bad=0)
    client.post("/v1/evals/run", json={"agent": "bot", "limit": 500})
    same = client.get("/v1/agents/bot/compare", params={"baseline": "v1", "candidate": "v2"}).json()
    assert same["overall_verdict"] == "no_change"
    thin = client.get("/v1/agents/bot/compare", params={"baseline": "v1", "candidate": "v3"}).json()
    assert thin["overall_verdict"] == "insufficient_data"


def test_compare_across_environments(client: TestClient):
    make_version(client, "bot", "v1")
    seed(client, "v1", good=30, bad=0, environment="production")
    seed(client, "v1", good=10, bad=20, environment="canary")
    client.post("/v1/evals/run", json={"agent": "bot", "limit": 500})
    cmp = client.get("/v1/agents/bot/compare", params={
        "baseline": "v1", "candidate": "v1", "baseline_environment": "production", "candidate_environment": "canary",
    }).json()
    assert cmp["overall_verdict"] == "regression"


def test_compare_validation(client: TestClient):
    make_version(client, "bot", "v1")
    base = {"baseline": "v1", "candidate": "v1"}
    assert client.get("/v1/agents/bot/compare", params=base).status_code == 422
    assert client.get("/v1/agents/bot/compare", params={"baseline": "v1", "candidate": "nope"}).status_code == 404
    assert client.get("/v1/agents/ghost/compare", params={"baseline": "v1", "candidate": "v2"}).status_code == 404
    assert client.get("/v1/agents/bot/compare", params={"baseline": "v1"}).status_code == 422
    assert client.get("/v1/agents/bot/compare", params={**base, "candidate": "v1", "alpha": 2}).status_code == 422


def test_runs_without_a_version_can_be_compared_as_unversioned(client: TestClient):
    make_version(client, "bot", "v1")
    for _ in range(25):
        client.post("/v1/runs", json={"agent": "bot", "output": "x"})  # no version -> active/none
    seed(client, "v1", good=25, bad=0)
    client.post("/v1/evals/run", json={"agent": "bot", "limit": 500})
    r = client.get("/v1/agents/bot/compare", params={"baseline": "(unversioned)", "candidate": "v1"})
    assert r.status_code == 200
