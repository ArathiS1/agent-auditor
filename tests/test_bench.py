"""The benchmark runner's question set, and the link between the chat's features and the gap matrix. No network, no keys."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "chatbot"), str(ROOT / "src"), str(ROOT / "bench")]

import run as bench  # noqa: E402
from agent_auditor.evals.expect import validate_text_expectation  # noqa: E402
from capabilities import FEATURES  # noqa: E402


def test_every_benchmark_question_has_valid_expectations():
    ids = set()
    for q in bench.QUESTIONS:
        assert q["id"] not in ids
        ids.add(q["id"])
        assert q["q"].strip() and q["category"] and q["expect"]
        for i, e in enumerate(q["expect"]):
            validate_text_expectation(i, e)  # raises on a bad key such as "kind" instead of "type"


def test_quick_set_covers_every_category_but_is_shorter():
    full, quick = bench.pick(False), bench.pick(True)
    assert len(quick) < len(full)
    assert {q["category"] for q in quick} == {q["category"] for q in full}


def test_models_come_only_from_keys_that_are_set(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    env = {"GROQ_API_KEY": "gsk_x" * 8}
    names = {m["provider"] for m in bench.choose_models([], env)}
    assert "groq" in names and "anthropic" not in names
    assert {m["provider"] for m in bench.choose_models(["groq"], env)} == {"groq"}


def test_every_feature_the_chat_claims_is_in_the_comparison_matrix():
    matrix = json.loads((ROOT / "src" / "agent_auditor" / "data" / "feature_matrix.json").read_text(encoding="utf-8"))
    known = {f["id"] for f in matrix["features"]}
    assert set(FEATURES) <= known, sorted(set(FEATURES) - known)
    assert all(len(f["values"]) == len(matrix["products"]) for f in matrix["features"])
