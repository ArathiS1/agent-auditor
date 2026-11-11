"""bench/canary.py: friendly refusals before it spends any model calls."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "bench"), str(ROOT / "chatbot"), str(ROOT / "src")]

import canary as C  # noqa: E402


def test_needs_both_models_and_a_key(capsys, monkeypatch):
    monkeypatch.delenv("AUDITOR_API_KEY", raising=False)
    assert C.main([]) == 2 and "Choose both" in capsys.readouterr().out
    assert C.main(["--baseline", "a", "--candidate", "b"]) == 2 and "AUDITOR_API_KEY" in capsys.readouterr().out
    assert C.main(["--baseline", "a", "--candidate", "b", "--key", "k", "--runs", "2"]) == 2


def test_refuses_unknown_or_identical_models(capsys, monkeypatch):
    monkeypatch.setattr(C, "pick_model", lambda w: None)
    assert C.main(["--baseline", "x", "--candidate", "y", "--key", "k"]) == 2
    assert "not found or has no key" in capsys.readouterr().out

    class P:
        name, model, ready = "gemini", "m", True

    monkeypatch.setattr(C, "pick_model", lambda w: P())
    assert C.main(["--baseline", "x", "--candidate", "y", "--key", "k"]) == 2
    assert "same model" in capsys.readouterr().out


def test_list_shows_models_with_a_key(capsys, monkeypatch):
    monkeypatch.setattr(C, "choose_models", lambda w: [{"id": "gemini:gemini-2.5-flash"}])
    assert C.main(["--list"]) == 0 and "gemini:gemini-2.5-flash" in capsys.readouterr().out


def test_break_candidate_allows_the_same_model(capsys, monkeypatch):
    """The planted-fault option compares a model with itself, so the 'same model' refusal must not fire."""
    from providers import Provider

    monkeypatch.setattr(C, "pick_model", lambda w: Provider("gemini", "openai", "http://x/v1", "GEMINI_API_KEY", "m"))
    args = ["--baseline", "x", "--candidate", "x", "--key", "k", "--auditor", "http://127.0.0.1:9"]
    assert C.main(args) == 2 and "same model" in capsys.readouterr().out
    assert C.main(args + ["--break-candidate"]) == 2
    assert "same model" not in capsys.readouterr().out


def test_verdicts_needs_the_auditor_key(capsys, monkeypatch):
    import verdicts as V

    monkeypatch.delenv("AUDITOR_API_KEY", raising=False)
    assert V.main([]) == 2 and "AUDITOR_API_KEY" in capsys.readouterr().out
