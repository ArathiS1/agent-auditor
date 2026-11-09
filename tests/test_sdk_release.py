"""SDK side of Phase 8: release calls post what the API expects."""
from agent_auditor.sdk import Auditor, AuditorError


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p, self.text = status, payload if payload is not None else {}, str(payload)

    def json(self):
        return self._p


class Http:
    def __init__(self, status=200, versions=None):
        self.calls, self.status, self.versions = [], status, versions or []

    def get(self, path, params=None, headers=None):
        self.calls.append(("GET", path, params))
        return Resp(self.status, self.versions if path.endswith("/versions") else [{"id": "r1"}])

    def post(self, path, json=None, headers=None):
        self.calls.append(("POST", path, json))
        return Resp(self.status, {"id": "r1", "state": "proposed"})

    def close(self):
        pass


def make(status=200, versions=None):
    http = Http(status, versions)
    return Auditor(agent="my bot", http_client=http, background=False, api_key="k"), http


def test_propose_sends_only_what_was_set():
    auditor, http = make()
    auditor.propose_release("v2", proposed_by="alice")
    assert http.calls[0] == ("POST", "/v1/agents/my%20bot/releases", {"candidate": "v2", "proposed_by": "alice"})
    auditor.propose_release("v2", proposed_by="alice", notes="n", policy={"required_approvals": 2})
    assert http.calls[1][2] == {"candidate": "v2", "proposed_by": "alice", "notes": "n", "policy": {"required_approvals": 2}}


def test_approve_and_reject():
    auditor, http = make()
    auditor.approve_release("r1", approver="bob", comment="ok")
    auditor.reject_release("r1", approver="carol")
    assert http.calls[0] == ("POST", "/v1/releases/r1/approve", {"approver": "bob", "decision": "approve", "comment": "ok"})
    assert http.calls[1][2] == {"approver": "carol", "decision": "reject"}


def test_promote_rollback_cancel():
    auditor, http = make()
    auditor.promote_release("r1", actor="alice")
    auditor.rollback_release("r1", actor="alice", reason="errors up")
    auditor.rollback_release("r1", actor="alice", reason="x", to_version="v0")
    auditor.cancel_release("r1", actor="alice")
    assert [c[1] for c in http.calls] == ["/v1/releases/r1/promote", "/v1/releases/r1/rollback",
                                         "/v1/releases/r1/rollback", "/v1/releases/r1/cancel"]
    assert http.calls[1][2] == {"actor": "alice", "reason": "errors up"}
    assert http.calls[2][2]["to_version"] == "v0"
    assert http.calls[3][2] == {"actor": "alice"}


def test_reads():
    auditor, http = make()
    auditor.get_release("r1")
    assert auditor.list_releases(state="proposed", limit=5) == [{"id": "r1"}]
    auditor.release_audit("r1")
    assert http.calls[1] == ("GET", "/v1/agents/my%20bot/releases", {"state": "proposed", "limit": 5})
    assert http.calls[2][1] == "/v1/releases/r1/audit"


def test_active_version_picks_the_active_one():
    auditor, _ = make(versions=[{"label": "v2", "is_active": False}, {"label": "v1", "is_active": True}])
    assert auditor.active_version()["label"] == "v1"
    auditor, _ = make(versions=[{"label": "v2", "is_active": False}])
    assert auditor.active_version() is None


def test_conflict_surfaces_as_auditor_error():
    auditor, _ = make(status=409)
    try:
        auditor.promote_release("r1", actor="alice")
    except AuditorError as exc:
        assert "409" in str(exc)
        return
    raise AssertionError("expected AuditorError")
