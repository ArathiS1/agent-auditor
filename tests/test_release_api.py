"""End-to-end tests for releases: gate, approvals, promotion, rollback, audit (in-memory SQLite)."""
from fastapi.testclient import TestClient

from test_canary_api import post_run


def setup(client: TestClient, *, canary_failures: int = 0, evaluate: bool = True) -> dict | None:
    for label, activate in (("v1", True), ("v2", False)):
        r = client.post("/v1/agents/bot/versions", json={"label": label, "prompt": label, "activate": activate})
        assert r.status_code in (200, 201), r.text
    for _ in range(60):
        post_run(client, version="v1", environment="production")
    for i in range(40):
        post_run(client, version="v2", environment="canary", fail=i < canary_failures)
    return evaluate_canary(client) if evaluate else None


def evaluate_canary(client: TestClient) -> dict:
    r = client.post("/v1/agents/bot/canary/evaluate", json={"baseline": "v1", "candidate": "v2"})
    assert r.status_code == 200, r.text
    return r.json()


def propose(client: TestClient, by="alice", **policy):
    return client.post("/v1/agents/bot/releases", json={"candidate": "v2", "proposed_by": by, "policy": policy})


def approve(client, rid, who="bob", decision="approve"):
    return client.post(f"/v1/releases/{rid}/approve", json={"approver": who, "decision": decision, "comment": "looks fine"})


def promote(client, rid, who="alice"):
    return client.post(f"/v1/releases/{rid}/promote", json={"actor": who})


def active_label(client) -> str | None:
    versions = client.get("/v1/agents/bot/versions").json()
    return next((v["label"] for v in versions if v["is_active"]), None)


def actions(client, rid) -> list[str]:
    return [e["action"] for e in client.get(f"/v1/releases/{rid}/audit").json()]


def test_full_flow_with_refusals_and_audit(client):
    assert setup(client)["verdict"] == "promote"
    r = propose(client)
    assert r.status_code == 201, r.text
    rel = r.json()
    rid = rel["id"]
    assert rel["state"] == "proposed" and rel["baseline"] == "v1" and rel["gate"]["evidence_ok"] is True
    assert rel["recommendation"] == "evidence is good; waiting for approval(s)"

    refused = promote(client, rid)
    assert refused.status_code == 409
    assert any(b.startswith("approvals") for b in refused.json()["detail"]["blockers"])
    assert client.post(f"/v1/releases/{rid}/approve", json={"approver": "alice"}).status_code == 403

    approved = approve(client, rid).json()
    assert [a["approver"] for a in approved["approvals"]] == ["bob"] and approved["recommendation"] == "ready to promote"

    done = promote(client, rid)
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["state"] == "promoted" and body["previous_active"] == "v1" and body["promoted_by"] == "alice"
    assert active_label(client) == "v2"
    assert actions(client, rid) == ["release.proposed", "release.promote_refused", "release.approved", "release.promoted"]


def test_bad_canary_blocks_approval_and_promotion(client):
    assert setup(client, canary_failures=16)["verdict"] == "rollback"
    rid = propose(client).json()["id"]
    r = approve(client, rid)
    assert r.status_code == 409 and "canary report says rollback" in r.json()["detail"]
    assert promote(client, rid).status_code == 409
    assert active_label(client) == "v1"
    assert client.get(f"/v1/releases/{rid}").json()["state"] == "proposed"
    assert "release.approval_refused" in actions(client, rid)


def test_no_canary_evidence_blocks(client):
    setup(client, evaluate=False)
    rid = propose(client).json()["id"]
    assert approve(client, rid).status_code == 409
    refused = promote(client, rid)
    assert refused.status_code == 409 and any("canary_evidence" in b for b in refused.json()["detail"]["blockers"])


def test_approval_goes_stale_when_new_evidence_is_bad(client):
    setup(client)
    rid = propose(client).json()["id"]
    assert approve(client, rid).status_code == 200
    for _ in range(40):
        post_run(client, version="v2", environment="canary", fail=True)
    assert evaluate_canary(client)["verdict"] == "rollback"
    detail = client.get(f"/v1/releases/{rid}").json()
    assert detail["gate"]["stale_approvers"] == ["bob"] and detail["gate"]["ready"] is False
    refused = promote(client, rid)
    assert refused.status_code == 409
    assert any("canary_verdict" in b for b in refused.json()["detail"]["blockers"])
    assert active_label(client) == "v1"


def test_two_approvals_required(client):
    setup(client)
    rid = propose(client, required_approvals=2).json()["id"]
    approve(client, rid, "bob")
    assert promote(client, rid).status_code == 409
    approve(client, rid, "bob")  # the same person again does not count twice
    assert promote(client, rid).status_code == 409
    approve(client, rid, "carol")
    assert promote(client, rid).status_code == 200


def test_rollback_restores_previous_version(client):
    setup(client)
    rid = propose(client).json()["id"]
    approve(client, rid)
    promote(client, rid)
    assert client.post(f"/v1/releases/{rid}/rollback", json={"actor": "alice", "reason": ""}).status_code == 422
    r = client.post(f"/v1/releases/{rid}/rollback", json={"actor": "alice", "reason": "error rate doubled"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "rolled_back" and body["restored"] == "v1" and body["closed_reason"] == "error rate doubled"
    assert active_label(client) == "v1"
    assert client.post(f"/v1/releases/{rid}/rollback", json={"actor": "alice", "reason": "again"}).status_code == 409
    assert actions(client, rid)[-1] == "release.rolled_back"


def test_rollback_needs_a_promoted_release(client):
    setup(client)
    rid = propose(client).json()["id"]
    assert client.post(f"/v1/releases/{rid}/rollback", json={"actor": "a", "reason": "x"}).status_code == 409


def test_reject_and_cancel_are_terminal(client):
    setup(client)
    rid = propose(client).json()["id"]
    rejected = approve(client, rid, "bob", decision="reject").json()
    assert rejected["state"] == "rejected" and rejected["closed_reason"] == "looks fine"
    assert promote(client, rid).status_code == 409
    assert approve(client, rid, "carol").status_code == 409

    again = propose(client).json()["id"]
    cancelled = client.post(f"/v1/releases/{again}/cancel", json={"actor": "alice", "reason": "wrong build"})
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert client.post(f"/v1/releases/{again}/cancel", json={"actor": "alice"}).status_code == 409


def test_proposal_validation(client):
    setup(client)
    assert client.post("/v1/agents/ghost/releases", json={"candidate": "v2", "proposed_by": "a"}).status_code == 404
    assert client.post("/v1/agents/bot/releases", json={"candidate": "nope", "proposed_by": "a"}).status_code == 404
    assert client.post("/v1/agents/bot/releases", json={"candidate": "v1", "proposed_by": "a"}).status_code == 409  # active
    assert propose(client).status_code == 201
    assert propose(client).status_code == 409  # one open release per candidate
    assert propose(client, required_approvals=0).status_code == 422
    assert client.post("/v1/agents/bot/releases", json={"candidate": "v2", "proposed_by": "  "}).status_code == 422
    assert client.get("/v1/releases/nope").status_code == 404
    assert client.get("/v1/releases/nope/audit").status_code == 404


def test_listing_and_global_audit(client):
    setup(client)
    rid = propose(client).json()["id"]
    approve(client, rid)
    promote(client, rid)
    listed = client.get("/v1/agents/bot/releases").json()
    assert [r["id"] for r in listed] == [rid]
    assert client.get("/v1/agents/bot/releases", params={"state": "rolled_back"}).json() == []
    events = client.get("/v1/audit", params={"action_prefix": "release."}).json()
    assert {e["action"] for e in events} >= {"release.proposed", "release.approved", "release.promoted"}
    assert all(e["entity_type"] == "release" for e in events)
    assert any(e["action"] == "canary.evaluate" for e in client.get("/v1/audit", params={"action_prefix": "canary"}).json())
