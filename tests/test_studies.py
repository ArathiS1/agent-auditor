"""The case-study runner in studies/: task set, scoring, report wording, and a dry run against the scripted fake model."""
import json
import sys
from pathlib import Path

import pytest

STUDIES = str(Path(__file__).resolve().parent.parent / "studies")
if STUDIES not in sys.path:
    sys.path.insert(0, STUDIES)

import study_report as report  # noqa: E402
import taskset  # noqa: E402


def test_every_task_has_a_checkable_answer_the_calculator_reproduces():
    assert len(taskset.TASKS) == 36 and len({t[0] for t in taskset.TASKS}) == 36
    for question, tool, args, expected in taskset.TASKS:
        got = taskset.calculate(**args) if tool == "calculate" else taskset.lookup_country(**args)
        assert taskset.is_correct(got, expected), question


def test_no_question_text_contains_another_so_the_fake_server_can_match_them():
    qs = [t[0] for t in taskset.TASKS]
    assert not any(a != b and a in b for a in qs for b in qs)


def test_sampling_is_repeatable_and_covers_the_set_before_repeating():
    assert taskset.sample(40) == taskset.sample(40)
    assert len({t[0] for t in taskset.sample(36)}) == 36


@pytest.mark.parametrize("answer, expected, ok", [
    ("It is about 397.8 dollars", "397.8", True), ("2,025.", "2025", True), ("Brasília", "Brasilia", True),
    ("I am not sure.", "43", False), ("The answer is 4300", "43", False), ("", "x", False), (None, "x", False),
    ("It is 20.0", "20", True), ("tokyo", "Tokyo", True),
])
def test_is_correct(answer, expected, ok):
    assert taskset.is_correct(answer, expected) is ok


def test_calculator_refuses_anything_but_arithmetic():
    assert taskset.calculate("__import__('os').system('id')").startswith("Error")
    assert taskset.calculate("9 ** 9999").startswith("Error")
    assert taskset.calculate("1/0").startswith("Error")


def test_fisher_exact_matches_known_values():
    assert report.fisher_two_sided(30, 30, 30, 30) == pytest.approx(1.0)
    assert report.fisher_two_sided(30, 30, 19, 30) < 0.001
    assert report.fisher_two_sided(3, 5, 1, 5) == pytest.approx(0.5238, abs=1e-3)   # the classic tea-tasting style 2x2


def _rows(n, correct, non_answers=0):
    rows = [{"question": f"q{i}", "expected": "1", "answer": "1", "error": None, "correct": True, "seconds": 1.0} for i in range(correct)]
    rows += [{"question": f"q{i}", "expected": "1", "answer": "I am not sure." if i < non_answers else "2", "error": None, "correct": False, "seconds": 1.0}
             for i in range(n - correct)]
    return rows


def _canary(verdict):
    return {"verdict": verdict, "reasons": ["a reason"], "checks": [{"name": "evals", "status": "pass", "reason": "fine"}]}


def _study(cand_correct, verdict, fake=False):
    return {"agent": "smolagents", "baseline": "m1", "candidate": "m2", "n": 30, "fake": fake, "date": "2026-01-01",
            "baseline_rows": _rows(30, 30), "candidate_rows": _rows(30, cand_correct, non_answers=3), "canary": _canary(verdict)}


def test_report_says_when_the_answer_key_saw_a_drop_the_canary_missed():
    text = report.build(_study(19, "promote"))
    assert "found a drop that the canary did not flag" in text and "19/30" in text and "p < 0.001" in text


def test_report_says_when_both_agree_or_neither_sees_anything():
    assert "agree" in report.build(_study(19, "rollback"))
    assert "Neither the answer key nor the canary found" in report.build(_study(29, "promote"))
    assert "flagged the candidate although the answer key shows no significant drop" in report.build(_study(29, "hold"))


def test_a_dry_run_is_labelled_and_a_real_run_is_not():
    assert "DRY RUN WITH A SCRIPTED FAKE MODEL" in report.build(_study(19, "promote", fake=True))
    assert "DRY RUN" not in report.build(_study(19, "promote"))
    assert "Limits" in report.build(_study(19, "promote"))


def test_non_answers_are_counted_only_among_short_answers_without_errors():
    rows = _rows(4, 0, non_answers=2)
    rows[2]["error"] = "boom"
    rows[2]["answer"] = "I am not sure."
    assert report.summarize(rows)["non_answers"] == 2 and report.summarize(rows)["errors"] == 1


def test_dry_run_end_to_end_with_a_stock_framework_agent(tmp_path):
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
    pytest.importorskip("openinference.instrumentation.langchain")
    pytest.importorskip("langchain_openai")
    pytest.importorskip("langgraph")
    import run_study

    code = run_study.main(["--fake", "--agent", "langgraph", "--runs", "6", "--out", str(tmp_path), "--port", "8779"])
    assert code == 0
    data = json.loads(next(tmp_path.glob("DRYRUN-*.json")).read_text())
    assert data["fake"] and len(data["baseline_rows"]) == 6 and len(data["candidate_rows"]) == 6
    assert all(r["correct"] for r in data["baseline_rows"])
    assert data["canary"]["baseline"]["runs"] == 6 and data["canary"]["candidate"]["runs"] == 6   # every trace became one run
    assert "DRY RUN" in next(tmp_path.glob("DRYRUN-*.md")).read_text()


def test_missing_key_and_identical_models_are_explained(monkeypatch, capsys):
    import run_study

    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    assert run_study.main(["--api-key-env", "NO_SUCH_KEY"]) == 2
    assert "--fake" in capsys.readouterr().out
    monkeypatch.setenv("NO_SUCH_KEY", "x")
    assert run_study.main(["--api-key-env", "NO_SUCH_KEY", "--baseline", "a", "--candidate", "a"]) == 2
