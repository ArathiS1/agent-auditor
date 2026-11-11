import sys

import pytest

from agent_auditor import __version__, cli


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "demo" in out and "serve" in out


def test_demo_fills_the_dashboard_with_a_story(monkeypatch, capsys):
    """Starts a real server on a spare port, loads the sample data and checks the story it tells."""
    import httpx

    for name in ("AUDITOR_DATABASE_URL", "AUDITOR_API_KEY", "AUDITOR_AUTO_CREATE_TABLES"):
        monkeypatch.setenv(name, "placeholder")  # so the demo's changes to os.environ are undone after the test
    monkeypatch.delitem(sys.modules, "agent_auditor.api.main", raising=False)
    seen = {}
    real_seed = __import__("agent_auditor.demo", fromlist=["seed"]).seed

    def spy(http, say=print):
        agent = real_seed(http, say)
        seen["canary"] = {c["candidate"]: c["verdict"] for c in http.get(f"/v1/agents/{agent}/canary").json()}
        seen["releases"] = {r["candidate"]: r["state"] for r in http.get(f"/v1/agents/{agent}/releases").json()}
        seen["agents"] = [a["name"] for a in http.get("/v1/agents").json()]
        return agent

    monkeypatch.setattr("agent_auditor.demo.seed", spy)
    assert cli.main(["demo", "--no-browser", "--exit-after-seed", "--port", "18765"]) == 0
    monkeypatch.delitem(sys.modules, "agent_auditor.api.main", raising=False)
    assert "/dashboard" in capsys.readouterr().out
    assert seen["agents"] == ["support-bot"]
    assert seen["canary"]["v2"] == "promote"       # the good candidate is allowed
    assert seen["canary"]["v3"] == "rollback"      # the bad one is stopped
    assert seen["releases"]["v2"] == "rolled_back"  # released, then rolled back by a person
    assert seen["releases"]["v3"] == "proposed"     # never approved or promoted
    with pytest.raises(httpx.HTTPError):            # the demo server is gone once the command returns
        httpx.get("http://127.0.0.1:18765/healthz", timeout=1)
