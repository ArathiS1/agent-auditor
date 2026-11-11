"""Answers that are wrong but well formed: the `gives_an_answer` heuristic, the optional `answer_judge`, and how both
reach the canary verdict. Runs carry no expected answers here, like real traffic."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from agent_auditor.evals import RunView, evaluate_run, judge
from agent_auditor.evals.scorers import default_scorer_names, get_scorers
from agent_auditor.rootcause.classify import classify_run


def score(name, view, params=None):
    results, errors = evaluate_run(view, [name], {name: params} if params else None)
    assert not errors, errors
    return results[name]


def run(output, **kw):
    return RunView(id="r", input="What is the capital of Australia?", output=output, **kw)


# ----------------------------------------------------------------- gives_an_answer
@pytest.mark.parametrize("text", [
    "I am not sure.", "I'm not sure about that.", "I don't know.", "Sorry, I can’t help with that.",
    "As an AI, I have no opinion.", "I'm sorry, but I cannot answer this.", "No idea!", "I do not have enough information.",
])
def test_non_answers_fail(text):
    r = score("gives_an_answer", run(text))
    assert r is not None and not r.passed and "non-answer" in r.reason


@pytest.mark.parametrize("text", [
    "The capital of Australia is Canberra.", "516", "Sydney.", "Yes, by chaining the two rules.",
])
def test_real_answers_pass_even_when_wrong(text):
    assert score("gives_an_answer", run(text)).passed


@pytest.mark.parametrize("text", [
    "No estoy seguro.", "No estoy segura de eso.", "No lo sé.", "Lo siento, pero no puedo ayudarte con eso.",
    "No tengo suficiente información.", "Como IA, no tengo opinión.",
    "Je ne suis pas sûr.", "Je ne sais pas.", "Désolé, mais je ne peux pas vous aider.",
    "Je n’ai pas assez d’informations.", "Aucune idée !", "En tant qu'IA, je n'ai pas d'avis.",
])
def test_spanish_and_french_non_answers_fail(text):
    r = score("gives_an_answer", run(text))
    assert r is not None and not r.passed and "non-answer" in r.reason


@pytest.mark.parametrize("text", [
    "La capital de Australia es Canberra.", "No, la capital es Canberra, no Sídney.", "Sí, se puede.",
    "La capitale de l'Australie est Canberra.", "Non, c'est Canberra, pas Sydney.", "Je sais : c'est Canberra.",
])
def test_spanish_and_french_real_answers_pass(text):
    assert score("gives_an_answer", run(text)).passed


def test_long_answers_with_a_hedge_still_count_as_answers():
    text = ("I don't know the exact figure, but based on the three reports I found the best estimate is about 391 units "
            "for the quarter, with a margin of roughly ten units either way depending on how returns are counted.")
    r = score("gives_an_answer", run(text))
    assert r.passed and "longer than a non-answer" in r.reason


def test_not_applicable_when_there_is_nothing_to_judge():
    assert score("gives_an_answer", run("I am not sure.", status="error", error="boom")) is None
    assert score("gives_an_answer", run("   ")) is None
    assert score("gives_an_answer", run(None)) is None
    # runs that carry expectations are judged by expectations_met, which knows the right answer
    assert score("gives_an_answer", run("I am not sure.", tags={"expect": [{"type": "output_contains", "value": "Canberra"}]})) is None
    assert score("gives_an_answer", run("I am not sure.", tags={"expect": []})) is not None


def test_registry_flags():
    specs = get_scorers()
    assert "gives_an_answer" in default_scorer_names()
    assert "answer_judge" in specs and "answer_judge" not in default_scorer_names()


def test_a_failed_non_answer_is_diagnosed_as_a_wrong_answer():
    d = classify_run(run("I am not sure."), extra_failed=["gives_an_answer"])
    assert d.primary == "wrong_answer" and any("non-answer" in e for f in d.findings for e in f.evidence)
    d = classify_run(run("Sydney."), extra_failed=["answer_judge"])
    assert d.primary == "wrong_answer" and any("judge" in e for f in d.findings for e in f.evidence)


# ----------------------------------------------------------------- answer_judge
def configure(monkeypatch, reply, seen=None):
    monkeypatch.setenv("AUDITOR_JUDGE_BASE_URL", "http://judge.test/v1")
    monkeypatch.setenv("AUDITOR_JUDGE_API_KEY", "k")
    monkeypatch.setenv("AUDITOR_JUDGE_MODEL", "judge-1")

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    monkeypatch.setattr(judge, "_TRANSPORT", httpx.MockTransport(handler))


def test_parse_verdict():
    assert judge.parse_verdict('{"score": 0.25, "reason": "wrong city"}') == (0.25, "wrong city")
    assert judge.parse_verdict('Sure!\n```json\n{"score": 1}\n```')[0] == 1.0
    assert judge.parse_verdict('{"score": 7}')[0] == 1.0  # clamped
    assert judge.parse_verdict("0.5") == (0.5, "")
    with pytest.raises(ValueError):
        judge.parse_verdict("I think it is fine.")


def test_judge_grades_and_sends_the_question_and_answer(monkeypatch):
    seen = []
    configure(monkeypatch, '{"score": 0.1, "reason": "Sydney is not the capital"}', seen)
    r = score("answer_judge", run("The capital is Sydney."))
    assert not r.passed and r.score == 0.1 and "Sydney" in r.reason
    sent = seen[0]
    assert sent["model"] == "judge-1" and sent["temperature"] == 0
    assert "capital of Australia" in sent["messages"][1]["content"] and "The capital is Sydney." in sent["messages"][1]["content"]
    assert "REFERENCE" not in sent["messages"][1]["content"]


def test_judge_uses_a_reference_answer_when_the_run_has_one(monkeypatch):
    seen = []
    configure(monkeypatch, '{"score": 1, "reason": "matches"}', seen)
    r = score("answer_judge", run("Canberra", tags={"reference": "Canberra"}))
    assert r.passed and r.details["has_reference"]
    assert "REFERENCE ANSWER:\nCanberra" in seen[0]["messages"][1]["content"]


def test_judge_threshold_is_a_parameter(monkeypatch):
    configure(monkeypatch, '{"score": 0.6, "reason": "partly right"}')
    assert score("answer_judge", run("x")).passed
    assert not score("answer_judge", run("x"), {"threshold": 0.8}).passed


def test_judge_skips_failed_or_empty_runs_without_calling_the_model(monkeypatch):
    seen = []
    configure(monkeypatch, '{"score": 1}', seen)
    assert score("answer_judge", run("x", status="error", error="boom")) is None
    assert score("answer_judge", run("")) is None
    assert seen == []


def test_an_unconfigured_or_confused_judge_is_a_scorer_error_not_an_agent_failure(monkeypatch):
    for name in ("AUDITOR_JUDGE_BASE_URL", "AUDITOR_JUDGE_API_KEY", "AUDITOR_JUDGE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    results, errors = evaluate_run(run("x"), ["answer_judge"])
    assert "answer_judge" not in results and "not configured" in errors["answer_judge"]
    configure(monkeypatch, "no idea what to say")
    results, errors = evaluate_run(run("x"), ["answer_judge"])
    assert "answer_judge" not in results and "no readable score" in errors["answer_judge"]


# ----------------------------------------------------------------- in the canary verdict
NOW = datetime.now(timezone.utc)
_n = [0]


def post(client, version, environment, output):
    _n[0] += 1
    started = NOW - timedelta(seconds=_n[0])
    r = client.post("/v1/runs", json={
        "agent": "qa", "version": version, "environment": environment, "status": "ok",
        "input": {"question": f"question number {_n[0] % 6}"}, "output": output,
        "started_at": started.isoformat(), "ended_at": (started + timedelta(milliseconds=300 + _n[0] % 5 * 10)).isoformat(),
        "steps": [{"kind": "llm", "name": "answer"}], "tags": {},
    })
    assert r.status_code == 201, r.text


def fill(client, canary_output, n=40):
    for label in ("v1", "v2"):
        assert client.post("/v1/agents/qa/versions", json={"label": label, "prompt": label, "activate": False}).status_code in (200, 201)
    for i in range(n):
        post(client, "v1", "production", f"Here is the answer to question {i % 6}: it is covered in the guide.")
        post(client, "v2", "canary", canary_output(i))


def canary(client, **body):
    r = client.post("/v1/agents/qa/canary/evaluate", json={"baseline": "v1", "candidate": "v2", **body})
    assert r.status_code == 200, r.text
    return r.json()


def check(report, name):
    return next((c for c in report["checks"] if c["name"] == name), None)


def test_canary_rolls_back_a_candidate_that_says_it_is_not_sure(client):
    fill(client, lambda i: "I am not sure.")
    report = canary(client)
    assert report["verdict"] == "rollback", report["reasons"]
    assert "evals" in report["blocking"] and "gives_an_answer" in check(report, "evals")["reason"]


def test_canary_without_the_new_check_promotes_the_same_candidate(client):
    """This is the gap the check closes: the older built-in scorers cannot see a non-answer."""
    fill(client, lambda i: "I am not sure.")
    old = [n for n in default_scorer_names() if n != "gives_an_answer"]
    assert canary(client, scorers=old)["verdict"] == "promote"


def test_confident_wrong_answers_need_the_judge(client, monkeypatch):
    fill(client, lambda i: f"Here is the answer to question {i % 6}: it is 42 and the guide says nothing else.")
    assert canary(client)["verdict"] == "promote"  # fluent, so no built-in heuristic objects

    def grade(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["messages"][1]["content"]
        wrong = "it is 42" in text
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"score": 0.0 if wrong else 1.0, "reason": "says 42, the guide says otherwise" if wrong else "fine"})}}]})

    monkeypatch.setenv("AUDITOR_JUDGE_BASE_URL", "http://judge.test/v1")
    monkeypatch.setenv("AUDITOR_JUDGE_MODEL", "judge-1")
    monkeypatch.setattr(judge, "_TRANSPORT", httpx.MockTransport(grade))
    report = canary(client, scorers=default_scorer_names() + ["answer_judge"])
    assert report["verdict"] == "rollback", report["reasons"]
    assert "answer_judge" in check(report, "evals")["reason"]
    assert check(report, "scorers") is None


def test_a_judge_that_cannot_run_is_reported_not_silently_dropped(client, monkeypatch):
    for name in ("AUDITOR_JUDGE_BASE_URL", "AUDITOR_JUDGE_API_KEY", "AUDITOR_JUDGE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    fill(client, lambda i: f"Here is the answer to question {i % 6}: it is covered in the guide.")
    report = canary(client, scorers=default_scorer_names() + ["answer_judge"])
    warn = check(report, "scorers")
    assert warn["status"] == "warn" and "answer_judge" in warn["details"]["errors"]
    assert any(w.startswith("scorers:") for w in report["warnings"])
    strict = canary(client, scorers=default_scorer_names() + ["answer_judge"], policy={"warnings_block": True}, save=False)
    assert strict["verdict"] == "hold"


# ----------------------------------------------------------------- the demo script
def test_the_offline_demo_tells_the_whole_story(monkeypatch):
    for name in ("AUDITOR_API_KEY", "AUDITOR_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "bench"))
    monkeypatch.syspath_prepend(str(root / "chatbot"))
    sys.modules.pop("run", None)  # bench/run.py, not another module called run
    import wrong_answers

    out = wrong_answers.run()
    assert [out["A"][k]["verdict"] for k in ("before", "now", "judge")] == ["promote", "rollback", "rollback"]
    assert [out["B"][k]["verdict"] for k in ("before", "now", "judge")] == ["promote", "promote", "rollback"]
    assert out["A"]["now"]["regressed"] == ["gives_an_answer"]
    assert "answer_judge" in out["B"]["judge"]["regressed"]
