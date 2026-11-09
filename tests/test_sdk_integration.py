"""End to end: SDK -> real FastAPI app (in memory). Needs the full dev install."""
from fastapi.testclient import TestClient

from agent_auditor.api.app import create_app
from agent_auditor.sdk import Auditor


def test_sdk_traces_land_in_the_server():
    http = TestClient(create_app(database_url="sqlite://"))
    auditor = Auditor(agent="support-bot", http_client=http, background=False)
    auditor.register_version("v1", prompt="p", config={"top_k": 4})

    @auditor.observe(kind="retrieval")
    def search(q):
        return ["doc-a", "doc-b"]

    with auditor.run(input="reset password?", tags={"channel": "web"}) as run:
        search("reset password?")
        run.output = "Use the reset link."

    try:
        with auditor.run(input="boom"):
            raise RuntimeError("tool crashed")
    except RuntimeError:
        pass

    runs = http.get("/v1/runs").json()
    assert len(runs) == 2
    errors = http.get("/v1/runs", params={"status": "error"}).json()
    assert len(errors) == 1 and errors[0]["version"] == "v1"
    detail = http.get(f"/v1/runs/{next(r['id'] for r in runs if r['status'] == 'ok')}").json()
    assert detail["steps"][0]["name"] == "search" and detail["steps"][0]["kind"] == "retrieval"
    assert detail["tags"] == {"channel": "web"} and detail["latency_ms"] is not None
    assert auditor.stats["sent"] == 2


def test_root_redirects_to_docs():
    http = TestClient(create_app(database_url="sqlite://"))
    r = http.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/docs"
