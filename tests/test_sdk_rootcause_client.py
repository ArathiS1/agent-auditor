from agent_auditor.sdk import Auditor, AuditorError


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p, self.text = status, payload if payload is not None else {}, str(payload)

    def json(self):
        return self._p


class Http:
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def get(self, path, params=None, headers=None):
        self.calls.append(("GET", path, params, headers))
        return Resp(self.status, {"cause": "tool_timeout"})

    def post(self, path, json=None, headers=None):
        self.calls.append(("POST", path, json, headers))
        return Resp(self.status, {"classified": 3})

    def close(self):
        pass


def make(status=200):
    http = Http(status)
    return Auditor(agent="my bot", http_client=http, background=False, api_key="k"), http


def test_diagnose_posts_only_the_options_that_were_set():
    auditor, http = make()
    assert auditor.diagnose(version="v2", limit=50)["classified"] == 3
    method, path, body, headers = http.calls[0]
    assert (method, path) == ("POST", "/v1/agents/my%20bot/diagnose") and headers == {"X-API-Key": "k"}
    assert body == {"environment": "production", "version": "v2", "limit": 50, "reclassify": False}


def test_root_causes_defaults_and_overrides():
    auditor, http = make()
    auditor.root_causes(since_hours=24, min_runs=10)
    _, path, body, _ = http.calls[0]
    assert path == "/v1/agents/my%20bot/root-causes"
    assert body["diagnose_first"] is True and body["since_hours"] == 24 and body["min_runs"] == 10 and "version" not in body


def test_single_run_diagnosis_and_taxonomy_use_get():
    auditor, http = make()
    assert auditor.diagnose_run("abc/123")["cause"] == "tool_timeout"
    auditor.root_cause_taxonomy()
    assert http.calls[0][1] == "/v1/runs/abc%2F123/diagnosis" and http.calls[1][1] == "/v1/root-causes/taxonomy"


def test_server_errors_raise():
    auditor, _ = make(status=404)
    try:
        auditor.diagnose()
    except AuditorError:
        return
    raise AssertionError("expected AuditorError")
