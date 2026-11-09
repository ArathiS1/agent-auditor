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
        self.calls.append((path, json, headers))
        return Resp(self.status, {"overall": "drift", "signals": []})

    def close(self):
        pass


def test_check_drift_posts_to_the_server_with_defaults():
    http = Http()
    auditor = Auditor(agent="my bot", http_client=http, background=False, api_key="k")
    assert auditor.check_drift(current_hours=6)["overall"] == "drift"
    path, body, headers = http.calls[0]
    assert path == "/v1/drift/check" and headers == {"X-API-Key": "k"}
    assert body["agent"] == "my bot" and body["current_hours"] == 6 and body["reference_hours"] == 168.0
    assert "version" not in body and body["environment"] == "production"


def test_check_drift_server_error_raises():
    auditor = Auditor(agent="x", http_client=Http(status=404), background=False)
    try:
        auditor.check_drift()
    except AuditorError:
        return
    raise AssertionError("expected AuditorError")
