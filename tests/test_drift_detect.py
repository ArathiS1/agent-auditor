import dataclasses
import random

from drift_helpers import BASE_TOPICS, NEW_TOPICS, make_runs

from agent_auditor.drift import Thresholds, detect_drift, extract_signals
from agent_auditor.evals import RunView, StepView


def windows(seed, n_ref=150, n_cur=50, **cur_kw):
    rng = random.Random(seed)
    return make_runs(n_ref, rng), make_runs(n_cur, rng, **cur_kw)


def by_name(report):
    return {s.name: s for s in report.signals}


def flagged(report, minimum="medium"):
    rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
    return {s.name for s in report.signals if rank.get(s.severity, -1) >= rank[minimum]}


def test_no_real_change_is_stable():
    for seed in (1, 2, 3):
        ref, cur = windows(seed)
        report = detect_drift(ref, cur, seed=seed)
        assert report.overall == "stable", [(s.name, s.severity, s.reason) for s in report.signals if s.severity != "none"]
        assert report.reference_runs == 150 and report.current_runs == 50


def test_latency_shift_is_reported_as_high_with_the_right_numbers():
    ref, cur = windows(11, latency=1500)
    report = detect_drift(ref, cur)
    row = by_name(report)["latency_ms"]
    assert report.overall == "drift" and row.severity == "high" and row.significant
    assert row.effect_name == "ks_statistic" and row.effect > 0.9
    assert row.current["median"] > 1.4 * row.reference["median"]
    assert "median" in row.reason and "psi" in row.details


def test_retrieval_misses_flag_the_empty_retrieval_rate():
    ref, cur = windows(12, miss_rate=0.35)
    row = by_name(detect_drift(ref, cur))["empty_retrieval"]
    assert row.kind == "rate" and row.severity == "high" and row.effect > 0.2
    assert row.reference["rate"] < 0.12 and row.current["rate"] > 0.2 and len(row.details["current_ci"]) == 2


def test_retrieval_score_drop_is_found():
    ref, cur = windows(13, score=0.5)
    assert "retrieval_top_score" in flagged(detect_drift(ref, cur))


def test_new_topics_show_up_in_term_drift_with_the_new_words():
    ref, cur = windows(14, topics=BASE_TOPICS[:2] + NEW_TOPICS * 3)
    row = by_name(detect_drift(ref, cur))["input_terms"]
    assert row.kind == "terms" and row.severity in ("medium", "high")
    more = [t["term"] for t in row.details["more_common"]]
    assert "refund" in more and "chargeback" in more
    top = row.details["more_common"][0]
    assert top["current_share"] > top["reference_share"]


def test_insufficient_data_when_a_window_is_too_small():
    ref, cur = windows(15, n_cur=10)
    report = detect_drift(ref, cur)
    assert report.overall == "insufficient_data" and report.signals == [] and "at least 30" in report.notes[0]


def test_signal_missing_in_one_window_is_insufficient_not_an_error():
    ref, cur = windows(16)
    no_tools = [dataclasses.replace(r, steps=tuple(s for s in r.steps if s.kind != "tool")) for r in cur]
    report = detect_drift(ref, no_tools)
    row = by_name(report)["tool_calls"]
    assert row.severity == "insufficient_data" and "step_error" in by_name(report)
    assert report.overall != "insufficient_data" and any("not compared" in n for n in report.notes)


def test_extra_pass_rates_such_as_eval_results():
    ref, cur = windows(17)
    report = detect_drift(ref, cur, extra_rates={
        "eval:retrieval_nonempty": ((95, 100), (60, 100)),
        "eval:rare": ((9, 10), (4, 10)),
    })
    rows = by_name(report)
    assert rows["eval:retrieval_nonempty"].severity == "high"
    assert rows["eval:rare"].severity == "insufficient_data"
    assert report.overall == "drift"


def test_alpha_is_split_across_testable_signals():
    ref, cur = windows(18)
    report = detect_drift(ref, cur, alpha=0.01)
    used = {s.alpha_used for s in report.signals if s.severity != "insufficient_data"}
    assert len(used) == 1 and 0 < used.pop() < 0.01 and any("split across" in n for n in report.notes)
    relaxed = detect_drift(ref, cur, alpha=0.01, correct_multiple=False)
    assert all(s.alpha_used == 0.01 for s in relaxed.signals)


def test_severity_follows_effect_size_not_just_significance():
    ref, cur = windows(19, latency=1500)
    lenient = detect_drift(ref, cur, thresholds=Thresholds(ks_medium=0.99, ks_high=0.995))
    assert by_name(lenient)["latency_ms"].severity == "low"


def test_report_is_deterministic():
    ref, cur = windows(20, topics=BASE_TOPICS[:2] + NEW_TOPICS * 3)
    a = dataclasses.asdict(detect_drift(ref, cur, seed=7))
    b = dataclasses.asdict(detect_drift(ref, cur, seed=7))
    assert a == b


def test_parameter_validation():
    ref, cur = windows(21)
    for kwargs in ({"min_samples": 1}, {"alpha": 0.0}, {"alpha": 1.0}):
        try:
            detect_drift(ref, cur, **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {kwargs}")


def test_extract_signals_on_a_crafted_run():
    run = RunView(
        id="r", status="error", input={"question": "How do I reset my password?"}, output={"answer": "Open settings."},
        latency_ms=1200,
        steps=(
            StepView("retrieval", "kb", output=[], attributes={"scores": [0.2, 0.6]}),
            StepView("retrieval", "faq", output=["a", "b"], attributes={"scores": [0.9]}),
            StepView("tool", "lookup", status="error"),
            StepView("llm", "answer"),
        ),
    )
    s = extract_signals(run)
    assert s.numeric["latency_ms"] == 1200 and s.numeric["step_count"] == 4
    assert s.numeric["retrieval_top_score"] == 0.9 and s.numeric["retrieval_doc_count"] == 2
    assert s.numeric["input_chars"] == len("How do I reset my password?") and s.numeric["output_chars"] == len("Open settings.")
    assert s.flags == {"run_error": True, "step_error": True, "empty_retrieval": True}
    assert s.bags["tool_calls"] == frozenset({"lookup"}) and {"reset", "password"} <= s.bags["input_terms"]


def test_signals_that_do_not_apply_are_absent():
    s = extract_signals(RunView(id="r"))
    assert s.numeric == {"step_count": 0.0} and s.flags == {"run_error": False} and s.bags == {}
