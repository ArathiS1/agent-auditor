"""Auto-routing: the chat steers around models that are slow or resting after a rate limit."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chatbot"))

import router as R  # noqa: E402
import server as S  # noqa: E402


def setup_function(_):
    R.reset()
    S._DEAD.clear()
    S._DISCOVERED["at"] = 0.0


def test_slow_and_resting_models_are_avoided():
    R.record_success("a:fast", 4)
    R.record_success("a:slow", 90)
    R.record_failure("a:busy", 429)
    assert not R.avoid("a:fast") and R.slow("a:slow") and R.cooling("a:busy")
    assert R.avoid("a:slow") and R.avoid("a:busy") and not R.avoid("a:unknown")
    assert "rate limited" in R.reason("a:busy") and "slow" in R.reason("a:slow")
    assert R.sort_key("a:fast") < R.sort_key("a:unknown") < R.sort_key("a:busy")


def test_success_clears_cooldown_and_repeated_failures_rest_longer():
    R.record_failure("m", 503)
    first = R.snapshot()[0]["resting_for"]
    R.record_failure("m", 503)
    assert R.snapshot()[0]["resting_for"] > first
    R.record_success("m", 3)
    assert not R.cooling("m")


def test_chain_prefers_healthy_quick_models(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g" * 20)
    monkeypatch.setenv("MISTRAL_API_KEY", "m" * 20)
    monkeypatch.setenv("GROQ_API_KEY", "q" * 20)
    monkeypatch.setattr(S, "_reachable", lambda ms: ms)
    ids = [m["id"] for m in S._alive(S._with_discovered(S.available_models())) if m["ready"] and m["provider"] != "ollama"]
    assert len(ids) >= 3
    picked, quick, other = ids[0], ids[1], ids[2]
    R.record_success(picked, 120)  # the chosen model is slow
    R.record_success(quick, 2)
    R.record_success(other, 30)
    for m in S._alive(S._with_discovered(S.available_models())):
        if m["id"] not in (picked, quick, other):
            R.record_success(m["id"], 80)  # any other ready model is slower than these three
    chain = S._fallback_chain(S.provider_for(picked))
    names = [f"{p.name}:{p.model}" for p in chain]
    assert names[0] == quick and names[1] == other and picked in names  # the slow one is demoted, not removed


def test_auto_route_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g" * 20)
    monkeypatch.setenv("MISTRAL_API_KEY", "m" * 20)
    monkeypatch.setenv("CHAT_AUTOROUTE", "off")
    monkeypatch.setattr(S, "_reachable", lambda ms: ms)
    ids = [m["id"] for m in S._alive(S._with_discovered(S.available_models())) if m["ready"] and m["provider"] != "ollama"]
    R.record_success(ids[0], 200)
    assert S._fallback_chain(S.provider_for(ids[0]))[0].name == ids[0].split(":")[0]
    assert not R.enabled()


def test_answers_are_timed_and_failures_recorded(monkeypatch):
    import httpx
    import agent as A
    import tools as T
    from store import Store
    from providers import resolve_provider
    from test_chatbot import Tmp, text_reply
    monkeypatch.setenv("GEMINI_API_KEY", "g" * 20)
    monkeypatch.setattr(S, "_reachable", lambda ms: ms)
    with Tmp() as d:
        agent = A.ChatAgent(Store(d / "chat.db"), T.Toolbox(), transport=httpx.MockTransport(lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text_reply("ok").encode())), provider=resolve_provider({"CHAT_PROVIDER": "gemini"}))
        cid = agent.store.create()
        p = S.provider_for("gemini")
        events = list(S._with_fallback(agent, cid, "hi", None, [], False, p, False))
        assert events[-1]["type"] == "done"
        assert R.average(f"{p.name}:{p.model}") is not None
