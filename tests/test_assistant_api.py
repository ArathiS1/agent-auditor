"""Helper and suggest-fix endpoints with a mocked Claude API. Needs the full dev install."""
import httpx
from fastapi.testclient import TestClient

from agent_auditor.api.app import create_app


def _app(key="sk-test", status=200):
    app = create_app(database_url="sqlite://", anthropic_api_key=key)
    app.state.assistant_transport = httpx.MockTransport(
        lambda r: httpx.Response(status, json={"content": [{"type": "text", "text": "Looks safe to promote."}]}))
    return TestClient(app)


def test_status_reports_whether_the_helper_is_on():
    assert _app().get("/v1/assistant/status").json()["enabled"] is True
    c = TestClient(create_app(database_url="sqlite://"))
    off = c.get("/v1/assistant/status").json()
    assert off["enabled"] is False and off["model"] is None and off["guide"] is True


def test_ask_returns_the_answer_and_off_uses_the_built_in_guide(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY",
              "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = _app().post("/v1/assistant/ask", json={"question": "is v2 safe?", "context": {"agent": "bot"}})
    assert r.status_code == 200 and r.json()["answer"] == "Looks safe to promote." and r.json()["source"] == "Anthropic"
    off = TestClient(create_app(database_url="sqlite://"))
    g = off.post("/v1/assistant/ask", json={"question": "why does the chat say 429 quota"})
    assert g.status_code == 200 and g.json()["source"] == "guide" and "429" in g.json()["answer"]


def test_upstream_failure_falls_back_to_the_guide_and_never_leaks_the_key(monkeypatch):
    for k in ("GEMINI_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    r = _app(status=500).post("/v1/assistant/ask", json={"question": "what is the failures page"})
    assert r.status_code == 200 and r.json()["source"] == "guide" and "sk-test" not in r.text


def test_the_next_service_answers_when_the_first_fails(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key-123456")
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if "anthropic" in str(req.url):
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "From Gemini."}}]})

    app = create_app(database_url="sqlite://", anthropic_api_key="sk-test")
    app.state.assistant_transport = httpx.MockTransport(handler)
    c = TestClient(app)
    assert c.get("/v1/assistant/status").json()["providers"][:2] == ["anthropic", "gemini"]
    r = c.post("/v1/assistant/ask", json={"question": "hello"})
    assert r.json() == {"answer": "From Gemini.", "source": "Gemini"}
    c.post("/v1/assistant/ask", json={"question": "again"})
    assert sum("anthropic" in u for u in seen) == 1  # the failed service is skipped for a few minutes


def test_rate_limit_returns_429():
    c = _app()
    c.app.state.rate_limiter.limit = 1
    assert c.post("/v1/assistant/ask", json={"question": "a"}).status_code == 200
    assert c.post("/v1/assistant/ask", json={"question": "b"}).status_code == 429


def test_ask_needs_the_api_key_when_one_is_set():
    app = create_app(database_url="sqlite://", api_key="secret", anthropic_api_key="sk-test")
    assert TestClient(app).post("/v1/assistant/ask", json={"question": "hi"}).status_code == 401


def test_suggest_fix_for_unknown_agent_is_404_and_for_a_known_one_returns_a_draft():
    c = _app()
    assert c.post("/v1/agents/nope/suggest-fix", json={}).status_code == 404
    c.post("/v1/agents", json={"name": "bot"})
    c.post("/v1/agents/bot/versions", json={"label": "v1", "prompt": "Be brief.", "config": {}, "activate": True})
    r = c.post("/v1/agents/bot/suggest-fix", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["suggestion"] == "Looks safe to promote." and body["version"] in ("v1", None) and "canary" in body["note"]
