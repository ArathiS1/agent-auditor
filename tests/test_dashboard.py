"""The dashboard page and the API-key security scheme. Needs the full dev install."""
import re

from fastapi.testclient import TestClient

from agent_auditor.api.app import create_app


def test_dashboard_is_public_html_with_a_nonce_policy(client):
    r = client.get("/dashboard")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
    assert f"style-src 'nonce-{nonce}'" in csp and "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert f'<script nonce="{nonce}">' in r.text and "__NONCE__" not in r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_dashboard_nonce_changes_per_request(client):
    a = client.get("/dashboard").headers["content-security-policy"]
    b = client.get("/dashboard").headers["content-security-policy"]
    assert a != b


def test_dashboard_page_needs_no_key_but_data_does():
    c = TestClient(create_app(database_url="sqlite://", api_key="secret"))
    assert c.get("/dashboard").status_code == 200
    assert c.get("/v1/agents").status_code == 401
    assert c.get("/v1/agents", headers={"X-API-Key": "secret"}).status_code == 200


def test_non_ascii_key_is_rejected_not_a_server_error():
    c = TestClient(create_app(database_url="sqlite://", api_key="secret"))
    assert c.get("/v1/agents", headers={"X-API-Key": "sécret".encode("latin-1")}).status_code == 401


def test_openapi_declares_the_api_key_scheme(client):
    schemes = client.get("/openapi.json").json()["components"]["securitySchemes"]
    assert any(s.get("type") == "apiKey" and s.get("name") == "X-API-Key" for s in schemes.values())


def test_dashboard_has_the_friendly_layer(client):
    page = client.get("/dashboard").text
    for needle in ("The tape", "Ask for help", "What to look for", "Action needed", "/v1/assistant/ask", "/suggest-fix", "/v1/insights/gap-analysis", "/v1/insights/benchmark", "views.failures"):
        assert needle in page
    assert "innerHTML" not in page  # untrusted text only ever goes in through textContent


def test_openapi_lists_the_assistant_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/assistant/ask" in paths and "/v1/agents/{name}/suggest-fix" in paths
