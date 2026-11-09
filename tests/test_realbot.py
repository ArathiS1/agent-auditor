"""The sample notes bot (realbot/): retrieval, declining, tracing, and the Claude call. No network, no server."""
import csv
import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx

from agent_auditor.sdk import Auditor

ROOT = Path(__file__).resolve().parents[1] / "realbot"
spec = importlib.util.spec_from_file_location("realbot_bot", ROOT / "bot.py")
B = importlib.util.module_from_spec(spec)
sys.modules["realbot_bot"] = B
spec.loader.exec_module(B)


def _traced_bot(**options):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"id": len(sent)})

    http = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    auditor = Auditor("http://test", agent="notes-bot", background=False, http_client=http)
    return B.build_bot(auditor=auditor, use_llm=False, **options), sent


def test_the_right_note_is_found_for_a_known_question():
    bot, _ = _traced_bot()
    hits = bot.search("What is hybrid search?")
    assert hits and "BM25" in hits[0][0].text
    assert 0 < hits[0][1] <= 1


def test_answers_a_question_and_declines_an_off_topic_one():
    bot, _ = _traced_bot()
    assert "BM25" in bot.ask("What is hybrid search?")
    assert bot.ask("How do I cook biryani?") == B.DECLINE


def test_each_question_is_one_run_with_a_search_step_and_an_answer_step():
    bot, sent = _traced_bot()
    bot.ask("What does a reranker do?")
    run = sent[0]
    assert run["status"] == "ok" and run["input"] == "What does a reranker do?"
    kinds = [(s["name"], s["kind"]) for s in run["steps"]]
    assert kinds == [("search", "retrieval"), ("answer", "llm")]
    search = run["steps"][0]
    assert search["attributes"]["num_docs"] == len(search["output"]) and search["attributes"]["scores"]


def test_a_declined_question_records_only_the_search_step():
    bot, sent = _traced_bot()
    bot.ask("Who invented the telephone?")
    assert [s["name"] for s in sent[0]["steps"]] == ["search"]
    assert sent[0]["output"] == B.DECLINE


def test_a_simulated_llm_outage_is_recorded_as_a_failed_run():
    bot, sent = _traced_bot(break_mode="llm", break_rate=1.0, seed=1)
    try:
        bot.ask("What is hybrid search?")
        raise AssertionError("expected the simulated outage")
    except B.RateLimitError:
        pass
    run = sent[0]
    assert run["status"] == "error" and "RateLimitError" in run["error"]
    assert run["steps"][-1]["name"] == "answer" and run["steps"][-1]["status"] == "error"


def test_a_simulated_search_outage_returns_nothing_and_declines():
    bot, sent = _traced_bot(break_mode="search", break_rate=1.0, seed=1)
    assert bot.ask("What is hybrid search?") == B.DECLINE
    assert sent[0]["steps"][0]["attributes"]["num_docs"] == 0


def test_the_claude_call_sends_the_notes_and_the_key_and_returns_the_text():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Hybrid search mixes BM25 and vectors."}]})

    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        bot = B.build_bot(use_llm=True)
        bot._http = httpx.Client(transport=httpx.MockTransport(handler))
        answer = bot.ask("What is hybrid search?")
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    assert answer == "Hybrid search mixes BM25 and vectors."
    assert seen["headers"]["x-api-key"] == "test-key"
    assert "BM25" in seen["body"]["messages"][0]["content"] and seen["body"]["temperature"] == 0


def test_the_sample_questions_are_a_usable_size_and_mixed():
    rows = list(csv.DictReader((ROOT / "questions.csv").open(encoding="utf-8")))
    assert 100 <= len(rows) <= 300
    assert {r["in_scope"] for r in rows} == {"yes", "no"}
    assert all(r["expected"] for r in rows if r["in_scope"] == "yes")
