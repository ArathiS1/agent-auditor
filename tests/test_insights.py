"""Dashboard insight endpoints: overview, failures, timeline, feedback, benchmark, gap analysis."""
from datetime import datetime, timedelta, timezone

from agent_auditor.insights.bench import BenchRun, advise, summarize
from agent_auditor.insights.gap import analyse


def _run(client, **kw):
    body = {"agent": "bot", "input": "what is 2+2", "output": "4", "status": "ok", "tags": {}, "steps": []}
    body.update(kw)
    r = client.post("/v1/runs", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_overview_counts_runs_errors_and_raises_alerts(client):
    for _ in range(6):
        _run(client)
    for _ in range(2):
        _run(client, status="error", error="429 rate limit exceeded", output=None)
    d = client.get("/v1/insights/overview").json()
    assert d["runs"] == 8 and d["errors"] == 2
    assert d["error_kinds"] == {"rate_limit": 2}
    assert d["agents"][0]["name"] == "bot"
    assert any(a["view"] == "failures" for a in d["alerts"])
    assert client.get("/v1/insights/overview?agent=nope").status_code == 404


def test_failures_ledger_names_cause_and_advice(client):
    _run(client, status="error", error="Rate limit reached for model", output=None,
         steps=[{"name": "model_call", "kind": "llm", "status": "error", "error": "429 rate limit"}])
    _run(client)
    d = client.get("/v1/insights/failures").json()
    assert d["count"] == 1
    item = d["items"][0]
    assert item["error_kind"] == "rate_limit"
    assert item["cause"] == "llm_error"
    assert any("backoff" in a.lower() for a in item["advice"])


def test_feedback_down_turns_a_good_looking_run_into_a_failure(client):
    _run(client, tags={"turn_id": "t1"})
    r = client.post("/v1/feedback", json={"agent": "bot", "turn_id": "t1", "rating": "down", "comment": "wrong"})
    assert r.status_code == 200
    d = client.get("/v1/insights/failures").json()
    assert d["count"] == 1 and d["items"][0]["cause"] == "user_rejected"
    assert client.post("/v1/feedback", json={"agent": "bot", "turn_id": "zzz", "rating": "up"}).status_code == 404
    assert client.post("/v1/feedback", json={"agent": "bot", "turn_id": "t1", "rating": "meh"}).status_code == 422


def test_timeline_buckets_cover_the_window(client):
    _run(client)
    d = client.get("/v1/insights/timeline?hours=6").json()
    assert d["step_seconds"] == 600
    assert sum(b["ok"] for b in d["buckets"]) == 1


def test_runs_list_filters_by_text_and_version(client):
    client.post("/v1/agents/bot/versions", json={"label": "v1", "prompt": "p", "config": {}})
    _run(client, input="refund policy?", version="v1")
    client.post("/v1/agents/bot/versions", json={"label": "v2", "prompt": "p2", "config": {}})
    _run(client, input="shipping time?", version="v2")
    assert len(client.get("/v1/runs?q=refund").json()) == 1
    assert len(client.get("/v1/runs?version=v1").json()) == 1


def test_run_tagged_with_expectation_is_scored_and_diagnosed(client):
    _run(client, input="capital of France?", output="Berlin", tags={"expect": [{"type": "output_contains", "value": "Paris"}]})
    client.post("/v1/evals/run", json={"agent": "bot"})
    d = client.get("/v1/insights/failures").json()
    assert d["items"][0]["cause"] == "wrong_answer"


def _bench(version, qid, passed, cat="facts", status="ok", lat=1000.0, minutes=0, answer="x"):
    return BenchRun(run_id=f"{version}{qid}{minutes}", version=version, target=f"p:{version}", question_id=qid, category=cat,
                    question="q", status=status, error="429 rate limit" if status == "error" else None, latency_ms=lat, passed=passed,
                    reason=None, answer=answer, tool_calls=0, started_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes))


def test_benchmark_summary_ranks_and_uses_latest_run_only():
    runs = [_bench("a", f"q{i}", i < 9) for i in range(10)] + [_bench("b", f"q{i}", i < 4) for i in range(10)]
    runs.append(_bench("b", "q9", True, minutes=5))  # a later run of the same question replaces the earlier one
    s = summarize(runs, current="b")
    assert [t["version"] for t in s["targets"]] == ["a", "b"]
    assert s["targets"][1]["quality"]["n"] == 10 and s["targets"][1]["quality"]["passed"] == 5
    assert any(a["area"] == "quality" and a["severity"] == "high" for a in s["advice"])
    assert s["question_count"] == 10


def test_benchmark_advice_flags_errors_and_single_target():
    runs = [_bench("a", f"q{i}", None, status="error") for i in range(6)]
    advice = advise(summarize(runs, "a")["targets"], "a")
    assert any(a["area"] == "reliability" and "backoff" in a["detail"] for a in advice)
    assert any(a["area"] == "coverage" for a in advice)


def test_benchmark_endpoint_empty_then_filled(client):
    assert client.get("/v1/insights/benchmark?agent=nobody").status_code == 404
    client.post("/v1/agents/chat-agent/versions", json={"label": "m1", "prompt": "p", "config": {}})
    _run(client, agent="chat-agent", version="m1", environment="test", input="2+2?", output="4",
         tags={"bench": True, "target": "x:m1", "question_id": "q1", "category": "math", "expect": [{"type": "output_contains", "value": "4"}]})
    client.post("/v1/evals/run", json={"agent": "chat-agent"})
    d = client.get("/v1/insights/benchmark").json()
    assert d["targets"][0]["quality"]["passed"] == 1 and d["targets"][0]["target"] == "x:m1"


def test_gap_analysis_reads_features_the_chat_declared(client):
    d = client.get("/v1/insights/gap-analysis").json()
    assert d["reported"] is False and d["covered"] == 0 and d["gaps"]
    client.post("/v1/agents/chat-agent/versions", json={"label": "c1", "prompt": "p", "config": {"features": ["streaming", "web_search"]}})
    d = client.get("/v1/insights/gap-analysis").json()
    assert d["reported"] is True and d["covered"] == 2
    assert "streaming" not in [g["id"] for g in d["gaps"]]
    assert d["unique"]


def test_gap_ranking_puts_widespread_cheap_features_first():
    r = analyse([])
    first = r["gaps"][0]
    assert first["others_yes"] == max(g["others_yes"] for g in r["gaps"])
