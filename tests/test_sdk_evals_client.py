from agent_auditor.sdk import Auditor, AuditorError


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p, self.text = status, payload or {}, str(payload)

    def json(self):
        return self._p


class Http:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def post(self, path, json=None, headers=None):
        self.calls.append(("POST", path, json, None, headers))
        return Resp(self.status, {"evaluated_runs": 3})

    def get(self, path, params=None, headers=None):
        self.calls.append(("GET", path, None, params, headers))
        return Resp(self.status, {"overall_verdict": "regression"})

    def close(self):
        pass


def make(status=200):
    http = Http(status)
    return Auditor(agent="my bot", http_client=http, background=False, api_key="k"), http


def test_run_evals_defaults_to_own_agent_and_omits_none_fields():
    auditor, http = make()
    assert auditor.run_evals(limit=50)["evaluated_runs"] == 3
    method, path, body, _, headers = http.calls[0]
    assert (method, path) == ("POST", "/v1/evals/run") and headers == {"X-API-Key": "k"}
    assert body["agent"] == "my bot" and body["limit"] == 50 and "version" not in body and "scorers" not in body


def test_compare_versions_builds_query_and_encodes_agent():
    auditor, http = make()
    out = auditor.compare_versions("v1", "v2", candidate_environment="canary", min_samples=10)
    assert out["overall_verdict"] == "regression"
    method, path, _, params, _ = http.calls[0]
    assert path == "/v1/agents/my%20bot/compare"
    assert params == {"baseline": "v1", "candidate": "v2", "min_samples": 10, "min_effect": 0.05, "alpha": 0.05, "candidate_environment": "canary"}


def test_server_errors_raise_auditor_error():
    auditor, _ = make(status=404)
    for call in (lambda: auditor.run_evals(), lambda: auditor.compare_versions("a", "b")):
        try:
            call()
        except AuditorError:
            continue
        raise AssertionError("expected AuditorError")


def test_disabled_auditor_raises_for_explicit_calls():
    auditor = Auditor(agent="x", enabled=False)
    try:
        auditor.run_evals()
    except AuditorError:
        return
    raise AssertionError("expected AuditorError")
