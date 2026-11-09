"""End-to-end tests for failure-to-test conversion and replay grading (in-memory SQLite).
Needs the full dev install (FastAPI, SQLAlchemy)."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

NOW = datetime.now(timezone.utc)
_counter = [0]


def post_run(client: TestClient, question: str, *, agent="bot", version="v1", environment="production",
             docs=3, fail=False, tags=None, output=None) -> dict:
    """One run: retrieval then answer. fail=True makes it a tool-timeout error run."""
    _counter[0] += 1
    started = NOW - timedelta(minutes=1000 - _counter[0])
    steps = [{"kind": "retrieval", "name": "kb", "output": ["article"] * docs,
              "attributes": {"num_docs": docs, "scores": [0.9] * min(docs, 1)}}]
    if fail:
        steps.append({"kind": "tool", "name": "payments_api", "status": "error", "error": "TimeoutError: payments api"})
    payload = {
        "agent": agent, "version": version, "environment": environment,
        "status": "error" if fail else "ok", "error": "TimeoutError: payments api" if fail else None,
        "input": {"question": question},
        "output": None if fail else (output or ("Here is the answer from the article." if docs else "I could not find that.")),
        "started_at": started.isoformat(), "ended_at": (started + timedelta(milliseconds=300)).isoformat(),
        "steps": steps, "tags": tags or {},
    }
    r = client.post("/v1/runs", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def register(client: TestClient, *labels: str, agent="bot") -> None:
    for label in labels:
        r = client.post(f"/v1/agents/{agent}/versions", json={"label": label, "prompt": label, "activate": False})
        assert r.status_code in (200, 201), r.text


REFUND = ["How do I get a refund for my payment?", "how do i  get a REFUND for my payment?", "How can I get a refund for my payment"]
TIMEOUT_Q = "Charge my card again please"


def seed_failures(client: TestClient) -> None:
    register(client, "v1", "v2", "v3", "v4")
    for q in REFUND:
        post_run(client, q, docs=0)           # retrieval returned nothing
    post_run(client, TIMEOUT_Q, fail=True)    # tool timeout
    for q in ("reset password", "change email", "cancel plan", "invoice copy"):
        post_run(client, q)                   # healthy runs


def generate(client: TestClient, **body):
    return client.post("/v1/agents/bot/tests/generate", json=body)


def cases_by_kind(client: TestClient) -> dict:
    cases = client.get("/v1/agents/bot/tests").json()
    out = {}
    for c in cases:
        out["timeout" if "run_succeeded" in c["failure"]["scorers"] else "refund"] = c
    return out


# ---------------------------------------------------------------- generation
def test_failures_become_deduplicated_cases_and_healthy_runs_do_not(client: TestClient):
    seed_failures(client)
    r = generate(client)
    assert r.status_code == 200, r.text
    report = r.json()
    assert len(report["created"]) == 2 and report["merged_duplicates"] == 2
    cases = cases_by_kind(client)
    refund, timeout = cases["refund"], cases["timeout"]
    assert refund["occurrences"] == 3 and refund["failure"]["scorers"] == ["retrieval_nonempty"]
    assert refund["expectations"][0]["scorer"] == "retrieval_nonempty" and refund["expectations"][0]["params"] == {"min_docs": 1}
    assert {e["scorer"] for e in timeout["expectations"]} >= {"run_succeeded", "no_step_errors"}
    assert timeout["failure"]["error_type"] == "TimeoutError" and timeout["failure"]["failed_steps"] == ["payments_api"]
    assert refund["origin_version"] == "v1" and refund["created_by"] == "auto" and refund["enabled"] is True


def test_generating_twice_is_idempotent_and_does_not_double_count(client: TestClient):
    seed_failures(client)
    generate(client)
    again = generate(client).json()
    assert again["created"] == [] and again["merged_duplicates"] == 0 and again["skipped"]["already_covered"] == 4
    assert cases_by_kind(client)["refund"]["occurrences"] == 3
    post_run(client, "how can I get a refund for my payment", docs=0)  # a new sighting is merged and counted
    third = generate(client).json()
    assert third["created"] == [] and third["merged_duplicates"] == 1
    assert cases_by_kind(client)["refund"]["occurrences"] == 4


def test_dry_run_saves_nothing_and_limit_is_respected(client: TestClient):
    seed_failures(client)
    preview = generate(client, dry_run=True).json()
    assert preview["dry_run"] is True and len(preview["created"]) == 2 and preview["created"][0]["id"] is None
    assert client.get("/v1/agents/bot/tests").json() == []
    limited = generate(client, limit=1).json()
    assert len(limited["created"]) == 1 and any("limit" in n for n in limited["notes"])


def test_redaction_masks_personal_data_before_it_is_stored(client: TestClient):
    register(client, "v1")
    post_run(client, "Refund order for jane.doe@example.com, card 4111 1111 1111 1111", docs=0)
    case = generate(client, redact=True).json()["created"][0]
    assert "[EMAIL]" in case["input"]["question"] and "[CARD]" in case["input"]["question"]
    visible = str([case["input"], case["name"], case["reference_output"]])
    assert "jane.doe" not in visible and "4111" not in visible


def test_unknown_agent_is_404_and_replay_runs_never_become_cases(client: TestClient):
    assert generate(client).status_code == 404  # 'bot' has not been created yet
    assert client.post("/v1/agents/ghost/tests/generate", json={}).status_code == 404
    register(client, "v1")
    post_run(client, "bad replay run", environment="test", docs=0, tags={"replay_token": "t", "case_id": "x"})
    assert generate(client, environment="test").json()["created"] == []


# ---------------------------------------------------------------- manual cases and editing
def test_pin_a_run_as_a_test_and_edit_it(client: TestClient):
    register(client, "v1")
    bad = post_run(client, "Where is my refund?", docs=0)
    good = post_run(client, "reset password")
    r = client.post(f"/v1/runs/{bad['id']}/to-test", json={"name": "refund lookup"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True and body["case"]["name"] == "refund lookup" and body["case"]["created_by"] == "manual"
    assert client.post(f"/v1/runs/{bad['id']}/to-test", json={}).json()["created"] is False  # same input: not duplicated
    assert client.post(f"/v1/runs/{good['id']}/to-test", json={}).status_code == 422          # nothing failed
    exp = [{"type": "output_contains", "value": "password"}]
    ok = client.post(f"/v1/runs/{good['id']}/to-test", json={"expectations": exp})
    assert ok.status_code == 200 and ok.json()["case"]["expectations"][0]["type"] == "output_contains"
    assert client.post("/v1/runs/nope/to-test", json={}).status_code == 404

    case_id = body["case"]["id"]
    assert client.patch(f"/v1/tests/{case_id}", json={"enabled": False, "notes": "flaky upstream"}).json()["enabled"] is False
    assert case_id not in [c["id"] for c in client.get("/v1/agents/bot/tests", params={"enabled": True}).json()]
    assert case_id in [c["id"] for c in client.get("/v1/agents/bot/tests").json()]
    assert client.patch(f"/v1/tests/{case_id}", json={"expectations": [{"type": "bogus"}]}).status_code == 422
    assert client.patch("/v1/tests/nope", json={"enabled": True}).status_code == 404
    assert client.get(f"/v1/tests/{case_id}").json()["notes"] == "flaky upstream"


# ---------------------------------------------------------------- replay grading
def replay_runs(client: TestClient, version: str, token: str, *, refund_ok: bool, timeout_ok: bool, skip=()):
    cases = cases_by_kind(client)
    if "refund" not in skip:
        post_run(client, cases["refund"]["input"]["question"], version=version, environment="test",
                 docs=3 if refund_ok else 0, tags={"replay_token": token, "case_id": cases["refund"]["id"]})
    if "timeout" not in skip:
        post_run(client, cases["timeout"]["input"]["question"], version=version, environment="test",
                 fail=not timeout_ok, tags={"replay_token": token, "case_id": cases["timeout"]["id"]})
    return cases


def test_replay_verdicts_track_fixed_still_failing_and_newly_failing(client: TestClient):
    seed_failures(client)
    generate(client)

    cases = replay_runs(client, "v2", "tok-v2", refund_ok=True, timeout_ok=False)
    r = client.post("/v1/agents/bot/replays", json={"version": "v2", "token": "tok-v2", "label": "first attempt"})
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["verdict"] == "fail" and first["version"] == "v2"
    s = first["summary"]
    assert (s["passed"], s["failed"], s["total"]) == (1, 1, 2)
    assert s["fixed"] == [cases["refund"]["id"]] and s["still_failing"] == [cases["timeout"]["id"]] and s["newly_failing"] == []
    by_case = {x["case_id"]: x for x in first["results"]}
    assert by_case[cases["timeout"]["id"]]["outcome"] == "fail"
    assert any(c["status"] == "fail" for c in by_case[cases["timeout"]["id"]]["checks"])

    replay_runs(client, "v3", "tok-v3", refund_ok=True, timeout_ok=True)
    second = client.post("/v1/agents/bot/replays", json={"version": "v3", "token": "tok-v3"}).json()
    assert second["verdict"] == "pass" and second["summary"]["pass_rate"] == 1.0
    assert second["summary"]["fixed"] == [cases["timeout"]["id"]]  # refund was already passing: not re-counted

    replay_runs(client, "v4", "tok-v4", refund_ok=False, timeout_ok=True)
    third = client.post("/v1/agents/bot/replays", json={"version": "v4", "token": "tok-v4"}).json()
    assert third["verdict"] == "fail" and third["summary"]["newly_failing"] == [cases["refund"]["id"]]

    listing = client.get("/v1/agents/bot/replays").json()
    assert [x["version"] for x in listing] == ["v4", "v3", "v2"] and listing[0]["results"] == []
    assert client.get(f"/v1/replays/{first['id']}").json()["label"] == "first attempt"


def test_missing_cases_make_the_verdict_incomplete_not_pass(client: TestClient):
    seed_failures(client)
    generate(client)
    cases = replay_runs(client, "v2", "t", refund_ok=True, timeout_ok=True, skip=("timeout",))
    r = client.post("/v1/agents/bot/replays", json={"version": "v2", "token": "t"}).json()
    assert r["verdict"] == "incomplete" and r["summary"]["missing"] == 1
    assert {x["case_id"]: x["outcome"] for x in r["results"]}[cases["timeout"]["id"]] == "missing"
    # replaying only a subset is allowed when the caller says which cases it covers
    subset = client.post("/v1/agents/bot/replays", json={"version": "v2", "token": "t", "case_ids": [cases["refund"]["id"]]}).json()
    assert subset["verdict"] == "pass" and subset["summary"]["total"] == 1


def test_disabled_cases_are_not_replayed(client: TestClient):
    seed_failures(client)
    generate(client)
    cases = cases_by_kind(client)
    client.patch(f"/v1/tests/{cases['timeout']['id']}", json={"enabled": False})
    replay_runs(client, "v2", "t", refund_ok=True, timeout_ok=True, skip=("timeout",))
    r = client.post("/v1/agents/bot/replays", json={"version": "v2", "token": "t"}).json()
    assert r["verdict"] == "pass" and r["summary"]["total"] == 1


def test_explicit_results_mapping_works_without_a_token(client: TestClient):
    seed_failures(client)
    generate(client)
    cases = cases_by_kind(client)
    runs = []
    for kind, ok in (("refund", True), ("timeout", True)):
        run = post_run(client, cases[kind]["input"]["question"], version="v2", environment="test", fail=not ok if kind == "timeout" else False,
                       docs=3)
        runs.append({"case_id": cases[kind]["id"], "run_id": run["id"]})
    r = client.post("/v1/agents/bot/replays", json={"version": "v2", "results": runs})
    assert r.status_code == 200 and r.json()["verdict"] == "pass"


def test_replay_input_validation(client: TestClient):
    seed_failures(client)
    generate(client)
    replay_runs(client, "v2", "t", refund_ok=True, timeout_ok=True)
    post = lambda **b: client.post("/v1/agents/bot/replays", json=b)  # noqa: E731
    assert post(version="v2").status_code == 422                                   # no token and no results
    assert post(version="nope", token="t").status_code == 404                      # version not registered
    assert post(version="v3", token="t").status_code == 422                        # runs were recorded under v2
    assert post(version="v2", token="t", case_ids=["ghost"]).status_code == 422    # unknown case id
    assert post(version="v2", results=[{"case_id": "ghost", "run_id": "x"}]).status_code == 422
    assert client.post("/v1/agents/ghost/replays", json={"token": "t"}).status_code == 404
    assert client.get("/v1/replays/nope").status_code == 404


def test_replay_with_no_cases_is_rejected(client: TestClient):
    register(client, "v1")
    post_run(client, "all fine")
    assert client.post("/v1/agents/bot/replays", json={"version": "v1", "token": "t"}).status_code == 422
