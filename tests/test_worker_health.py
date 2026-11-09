"""Health endpoints and the background worker (in-memory SQLite). Needs the full dev install."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from agent_auditor import worker
from agent_auditor.models import EvalResult, RunDiagnosis

NOW = datetime.now(timezone.utc)
_n = [0]
TIMEOUT = "TimeoutError: payments api timed out"


def post_run(client, *, agent="bot", environment="production", fail=False) -> str:
    _n[0] += 1
    started = NOW - timedelta(seconds=_n[0])
    payload = {
        "agent": agent, "environment": environment, "status": "error" if fail else "ok",
        "error": TIMEOUT if fail else None, "input": {"question": "q"}, "output": None if fail else "an answer",
        "started_at": started.isoformat(), "ended_at": (started + timedelta(milliseconds=300)).isoformat(),
        "steps": [
            {"kind": "retrieval", "name": "kb", "output": ["a"] * 3, "attributes": {"num_docs": 3, "scores": [0.9]}},
            {"kind": "tool", "name": "payments_api", "status": "error" if fail else "ok", "error": TIMEOUT if fail else None},
        ],
    }
    r = client.post("/v1/runs", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def counts(client) -> tuple[int, int]:
    with client.app.state.session_factory() as db:
        return (
            int(db.scalar(select(func.count(func.distinct(EvalResult.run_id)))) or 0),
            int(db.scalar(select(func.count(RunDiagnosis.id))) or 0),
        )


def test_health_endpoints_need_no_key(client):
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/readyz").json() == {"status": "ok"}


def test_health_endpoints_stay_open_when_a_key_is_set():
    from fastapi.testclient import TestClient

    from agent_auditor.api.app import create_app

    c = TestClient(create_app(database_url="sqlite://", api_key="secret"))
    assert c.get("/healthz").status_code == 200 and c.get("/readyz").status_code == 200
    assert c.get("/v1/agents/bot/releases").status_code == 401


def test_worker_scores_and_diagnoses_new_runs_once(client):
    for fail in (False, False, False, True, True):
        post_run(client, fail=fail)
    post_run(client, environment="canary")
    post_run(client, environment="test")  # replay runs are not the worker's business
    assert counts(client) == (0, 0)

    first = worker.run_once(client.app.state.session_factory)
    assert first == {"evaluated": 6, "diagnosed": 6, "errors": 0}
    assert counts(client) == (6, 6)

    second = worker.run_once(client.app.state.session_factory)
    assert second == {"evaluated": 0, "diagnosed": 0, "errors": 0}

    with client.app.state.session_factory() as db:
        causes = {c for (c,) in db.execute(select(RunDiagnosis.cause).where(RunDiagnosis.status == "failed"))}
    assert causes == {"tool_timeout"}


def test_worker_batch_limit(client):
    for _ in range(5):
        post_run(client)
    assert worker.run_once(client.app.state.session_factory, batch=2)["evaluated"] == 2
    assert worker.run_once(client.app.state.session_factory, batch=2)["evaluated"] == 2
    assert worker.run_once(client.app.state.session_factory, batch=2)["evaluated"] == 1


def test_one_failing_agent_does_not_stop_the_others(client, monkeypatch):
    post_run(client, agent="bad")
    post_run(client, agent="good")
    real = worker._process

    def flaky(db, agent, environment, batch):
        if agent == "bad":
            raise RuntimeError("boom")
        return real(db, agent, environment, batch)

    monkeypatch.setattr(worker, "_process", flaky)
    result = worker.run_once(client.app.state.session_factory)
    assert result["errors"] == 2  # both environments of the bad agent
    assert result["evaluated"] == 1 and counts(client) == (1, 1)


def test_main_once_exits_zero_and_rejects_bad_arguments(tmp_path, monkeypatch):
    from agent_auditor.api.app import create_app

    url = f"sqlite:///{tmp_path / 'w.db'}"
    create_app(database_url=url)  # creates the tables
    monkeypatch.setattr(worker, "get_settings", lambda: type("S", (), {"database_url": url})())
    assert worker.main(["--once"]) == 0
    try:
        worker.main(["--once", "--batch", "0"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("expected argparse to reject --batch 0")


def test_missing_schema_is_reported_not_raised():
    from agent_auditor.db import make_engine, make_session_factory

    factory = make_session_factory(make_engine("sqlite://"))  # no tables
    assert worker.run_once(factory)["errors"] == 1
