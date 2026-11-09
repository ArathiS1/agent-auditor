from fastapi.testclient import TestClient

V1 = {"label": "v1", "prompt": "You are helpful.", "config": {"model": "m1", "top_k": 4}}


def test_register_is_idempotent_on_content(client: TestClient):
    first = client.post("/v1/agents/bot/versions", json=V1)
    assert first.status_code == 201
    again = client.post("/v1/agents/bot/versions", json={**V1, "label": "renamed"})
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]


def test_label_clash_with_different_content_is_409(client: TestClient):
    client.post("/v1/agents/bot/versions", json=V1)
    r = client.post("/v1/agents/bot/versions", json={**V1, "prompt": "Different."})
    assert r.status_code == 409


def test_only_one_active_version_and_runs_follow_it(client: TestClient):
    client.post("/v1/agents/bot/versions", json=V1)
    client.post("/v1/agents/bot/versions", json={**V1, "label": "v2", "prompt": "Be concise."})
    versions = client.get("/v1/agents/bot/versions").json()
    assert [v["label"] for v in versions if v["is_active"]] == ["v2"]

    assert client.post("/v1/runs", json={"agent": "bot"}).json()["version"] == "v2"
    assert client.post("/v1/runs", json={"agent": "bot", "version": "v1"}).json()["version"] == "v1"


def test_run_with_unknown_version_is_404(client: TestClient):
    client.post("/v1/agents/bot/versions", json=V1)
    assert client.post("/v1/runs", json={"agent": "bot", "version": "nope"}).status_code == 404


def test_api_key_enforced_when_configured():
    secured = TestClient(create_app_with_key("s3cret"))
    assert secured.get("/v1/agents").status_code == 401
    assert secured.get("/v1/agents", headers={"X-API-Key": "wrong"}).status_code == 401
    assert secured.get("/v1/agents", headers={"X-API-Key": "s3cret"}).status_code == 200


def create_app_with_key(key: str):
    from agent_auditor.api.app import create_app
    return create_app(database_url="sqlite://", api_key=key)
