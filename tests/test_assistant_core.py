"""Prompt building, rate limiting and the Claude API call (mock transport, no network)."""
import json

import httpx

from agent_auditor import assistant


def _transport(status=200, payload=None, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=payload if payload is not None else {"content": [{"type": "text", "text": "hello"}]})
    return httpx.MockTransport(handler)


def test_build_messages_wraps_data_and_keeps_roles_alternating():
    msgs = assistant.build_messages("is v2 safe?", {"agent": "bot"}, [
        {"role": "assistant", "content": "stray"}, {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert "<dashboard_data>" in msgs[-1]["content"] and "is v2 safe?" in msgs[-1]["content"]


def test_build_messages_merges_a_trailing_user_turn_and_rejects_empty():
    msgs = assistant.build_messages("q", {}, [{"role": "user", "content": "earlier"}])
    assert len(msgs) == 1 and msgs[0]["content"].startswith("earlier")
    try:
        assistant.build_messages("   ", {})
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_context_is_truncated():
    out = assistant.trim_context({"x": "a" * 100_000}, 500)
    assert len(out) < 600 and out.endswith("[truncated]")


def test_rate_limiter_blocks_then_recovers():
    t = [0.0]
    rl = assistant.RateLimiter(limit=2, window=10, clock=lambda: t[0])
    rl.check(); rl.check()
    try:
        rl.check()
    except assistant.RateLimited:
        pass
    else:
        raise AssertionError("expected RateLimited")
    t[0] = 11.0
    rl.check()


def test_complete_sends_key_model_and_system_and_returns_text():
    seen = []
    out = assistant.complete(api_key="k", model="m", system="sys", messages=[{"role": "user", "content": "x"}],
                             transport=_transport(seen=seen))
    assert out == "hello"
    req = seen[0]
    body = json.loads(req.content)
    assert req.headers["x-api-key"] == "k" and body["model"] == "m" and body["system"] == "sys"


def test_complete_errors_are_safe_and_specific():
    for call, exc in [
        (lambda: assistant.complete(api_key=None, model="m", system="s", messages=[]), assistant.AssistantUnavailable),
        (lambda: assistant.complete(api_key="secret-key", model="m", system="s", messages=[], transport=_transport(401, {})), assistant.AssistantError),
        (lambda: assistant.complete(api_key="k", model="m", system="s", messages=[], transport=_transport(200, {"content": []})), assistant.AssistantError),
    ]:
        try:
            call()
        except exc as e:
            assert "secret-key" not in str(e)
        else:
            raise AssertionError("expected an error")


def test_fix_messages_includes_version_and_causes():
    report = {"total_runs": 100, "failed_runs": 9, "failure_rate": 0.09,
              "causes": [{"cause": "tool_timeout", "count": 7, "share_of_failures": 0.77, "top_steps": [{"name": "search", "count": 7}]}]}
    msg = assistant.fix_messages("v1", "Be brief.", {"top_k": 4}, report)[0]["content"]
    assert "tool_timeout" in msg and "Be brief." in msg and "<failure_data>" in msg


def test_guide_retrieval_finds_the_right_section_without_any_ai():
    assert "validation-" in assistant.answer_from_guide("why are there 599 failures for validation agent")
    assert "429" in assistant.answer_from_guide("what does 429 quota rate limit mean")
    assert "Canary" in assistant.answer_from_guide("what is a canary")
    assert "could not find" in assistant.answer_from_guide("zzzz qqqq")


def test_placeholder_keys_do_not_count_as_configured():
    env = {"GEMINI_API_KEY": "your key", "MISTRAL_API_KEY": "real-key-1", "XAI_API_KEY": "xai"}
    assert assistant.configured_providers(env, anthropic_key="") == ["mistral"]


def test_a_service_described_in_env_is_used_by_the_helper():
    env = {"INDOS_API_KEY": "key-12345", "INDOS_BASE_URL": "https://api.indos.example/v1", "INDOS_MODEL": "m1"}
    assert assistant.configured_providers(env, anthropic_key="") == ["indos"]
