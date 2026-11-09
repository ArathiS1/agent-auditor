"""Pure tests for root-cause classification (no server needed)."""
import json
import pathlib

from agent_auditor.evals import RunView, StepView
from agent_auditor.rootcause import classify_run, error_kind, summarize, version_associations
from agent_auditor.rootcause.benchmark import run_benchmark
from agent_auditor.rootcause.errors import add_error_pattern, clear_custom_patterns

DATA = pathlib.Path(__file__).parent / "data"


def retrieval(n_docs=3, top=0.8, status="ok", error=None, name="kb"):
    attrs = {"num_docs": n_docs, "scores": [top] if n_docs else []}
    return StepView("retrieval", name, status, error, "q", ["doc about billing refund"] * n_docs, attrs, 80.0)


def tool(status="ok", error=None, name="payments"):
    return StepView("tool", name, status, error, {}, None, {}, 120.0)


def llm(status="ok", error=None, ms=600.0):
    return StepView("llm", "answer", status, error, "p", None if status == "error" else "t", {}, ms)


def run(steps, *, status="ok", error=None, output="billing refund answer from the docs", latency=900.0):
    return RunView("r", status=status, error=error, input="q", output=output, latency_ms=latency, steps=tuple(steps))


# ---------------------------------------------------------------- error kinds
def test_error_kinds_for_common_messages():
    cases = {
        "httpx.ReadTimeout: The read operation timed out": "timeout",
        "504 Gateway Time-out": "timeout",
        "openai.RateLimitError: Error code: 429": "rate_limit",
        "This model's maximum context length is 8192 tokens": "context_length",
        "Incorrect API key provided": "auth",
        "Connection refused": "connection",
        "503 Service Unavailable": "server_error",
        "404 Client Error: Not Found": "not_found",
        "KeyError: 'customer_id'": "code_or_input",
        "Exception": "other", "failed": "other", "": "other", None: "other",
    }
    for text, kind in cases.items():
        assert error_kind(text) == kind, (text, error_kind(text))


def test_custom_patterns_extend_the_matcher_and_reject_unknown_kinds():
    assert error_kind("celery.exceptions.SoftTimeLimitExceeded") == "other"
    add_error_pattern("timeout", r"softtimelimitexceeded")
    try:
        assert error_kind("celery.exceptions.SoftTimeLimitExceeded") == "timeout"
    finally:
        clear_custom_patterns()
    assert error_kind("celery.exceptions.SoftTimeLimitExceeded") == "other"
    try:
        add_error_pattern("not-a-kind", "x")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_independent_message_sets_stay_above_their_measured_floors():
    """Two sets of 270 realistic messages written without sight of the matcher. Set A was used to
    fix gaps (so it scores high); set B was never used for tuning and is the honest figure."""
    def score(path):
        data = json.loads(path.read_text(encoding="utf-8"))
        total = sum(len(v) for v in data.values())
        return sum(error_kind(m) == k for k, v in data.items() for m in v) / total

    assert score(DATA / "error_messages_a.json") >= 0.93
    assert score(DATA / "error_messages_b.json") >= 0.80


# ---------------------------------------------------------------- one rule at a time
def test_healthy_run_has_no_diagnosis():
    d = classify_run(run([retrieval(), tool(), llm()]))
    assert d.status == "healthy" and d.primary is None and d.findings == ()


def test_tool_timeout_vs_tool_error_follow_the_error_text():
    t = classify_run(run([retrieval(), tool("error", "TimeoutError: payments api timed out")], status="error", error="x", output=None))
    assert (t.primary, t.strength, t.subcause, t.step) == ("tool_timeout", "strong", "timeout", "payments")
    e = classify_run(run([retrieval(), tool("error", "ConnectionError: Connection refused")], status="error", error="x", output=None))
    assert (e.primary, e.subcause) == ("tool_error", "connection")
    opaque = classify_run(run([retrieval(), tool("error", "Exception")], status="error", error="Exception", output=None))
    assert opaque.primary == "tool_error" and opaque.subcause is None
    silent = classify_run(run([retrieval(), tool("error", None)], status="error", error="tool failed", output=None))
    assert silent.primary == "tool_error" and silent.strength == "moderate"


def test_llm_and_retrieval_errors_use_the_step_kind_and_text_gives_the_subcause():
    d = classify_run(run([retrieval(), llm("error", "openai.RateLimitError: Error code: 429")], status="error", error="x", output=None))
    assert (d.primary, d.subcause) == ("llm_error", "rate_limit")
    d = classify_run(run([retrieval(status="error", error="Qdrant connection refused")], status="error", error="x", output=None))
    assert (d.primary, d.subcause) == ("retrieval_error", "connection")
    d = classify_run(run([retrieval(), llm("error", "Request timed out.")], status="error", error="x", output=None))
    assert (d.primary, d.subcause) == ("llm_error", "timeout")


def test_run_error_without_a_failing_step_is_an_agent_exception():
    d = classify_run(run([retrieval(), tool(), llm()], status="error", error="ValueError: could not parse model output", output=None))
    assert (d.primary, d.subcause, d.step) == ("agent_exception", "code_or_input", None)


def test_quality_symptoms():
    assert classify_run(run([retrieval(0), tool(), llm()], output="I could not find that")).primary == "retrieval_empty"
    low = classify_run(run([retrieval(3, top=0.3), tool(), llm()]))
    assert low.primary == "retrieval_low_relevance" and "0.30" in low.explanation
    ungrounded = classify_run(run([retrieval(), tool(), llm()], output="Certainly galaxy volcano orchestra saddle lantern"))
    assert (ungrounded.primary, ungrounded.strength) == ("ungrounded_answer", "weak")
    slow = classify_run(run([retrieval(), tool(), llm(ms=16000.0)], latency=17000.0))
    assert (slow.primary, slow.step, slow.subcause) == ("slow_run", "answer", "llm")
    assert classify_run(run([retrieval(), tool(), llm()], output="")).primary == "empty_output"


def test_unmatched_failures_are_unknown_not_guessed():
    d = classify_run(run([retrieval(), tool(), llm()]), extra_failed=["my_custom_judge"])
    assert d.status == "failed" and d.primary == "unknown" and "my_custom_judge" in d.explanation


# ---------------------------------------------------------------- precedence
def test_errors_beat_quality_symptoms_and_the_earliest_failing_step_wins():
    both = classify_run(run([retrieval(0), tool("error", "timed out")], status="error", error="x", output=None))
    assert both.primary == "tool_timeout" and [f.cause for f in both.findings] == ["tool_timeout", "retrieval_empty"]
    first = classify_run(run([retrieval(status="error", error="boom"), tool("error", "ValueError: x")], status="error", error="x", output=None))
    assert first.primary == "retrieval_error"
    second = classify_run(run([retrieval(), tool("error", "KeyError: 'a'"), llm("error", "429")], status="error", error="x", output=None))
    assert second.primary == "tool_error" and second.findings[1].cause == "llm_error"


def test_upstream_quality_symptoms_come_first():
    d = classify_run(run([retrieval(0), tool(), llm(ms=16000.0)], output="", latency=17000.0))
    assert d.primary == "retrieval_empty"
    assert {f.cause for f in d.findings} >= {"retrieval_empty", "slow_run"}


def test_thresholds_passed_in_params_are_respected():
    r = run([retrieval(3, top=0.6), tool(), llm()])
    assert classify_run(r).status == "healthy"
    assert classify_run(r, params={"retrieval_top_score": {"threshold": 0.7}}).primary == "retrieval_low_relevance"


# ---------------------------------------------------------------- aggregation
def row(i, cause, version="v1", step=None, strength="strong", subcause=None):
    return {"run_id": f"r{i}", "version": version, "status": "failed" if cause else "healthy", "cause": cause,
            "strength": strength if cause else None, "subcause": subcause, "step": step}


def test_summary_counts_shares_steps_and_examples():
    rows = [row(1, "tool_timeout", step="payments"), row(2, "tool_timeout", step="payments"), row(3, "tool_error", step="crm"),
            row(4, None), row(5, None)]
    s = summarize(rows)
    assert (s["total_runs"], s["failed_runs"], s["healthy_runs"], s["failure_rate"]) == (5, 3, 2, 0.6)
    top = s["causes"][0]
    assert top["cause"] == "tool_timeout" and top["count"] == 2 and top["share_of_failures"] == 0.6667
    assert top["top_steps"] == [{"name": "payments", "count": 2}] and top["example_run_ids"] == ["r1", "r2"]
    assert top["advice"] and top["meaning"]
    assert summarize([])["failure_rate"] is None


def test_version_association_flags_a_cause_concentrated_in_one_version():
    rows = [row(i, "retrieval_empty" if i < 30 else None, "v2") for i in range(100)]            # 30% in v2
    rows += [row(1000 + i, "retrieval_empty" if i < 2 else None, "v1") for i in range(200)]      # 1% in v1
    out = version_associations(rows)
    hit = out["associations"][0]
    assert hit["cause"] == "retrieval_empty" and hit["version"] == "v2" and hit["direction"] == "more_common"
    assert hit["rate_in_version"] == 0.3 and hit["p_value"] < 1e-6
    assert len(out["associations"]) == 1  # the mirror finding for v1 is not repeated
    three = rows + [row(2000 + i, "retrieval_empty" if i < 2 else None, "v3") for i in range(200)]
    out3 = version_associations(three)
    assert [(a["version"], a["direction"]) for a in out3["associations"]] == [("v2", "more_common")]  # v1, v3 are not outliers
    two_bad = [row(i, "retrieval_empty" if i < 30 else None, "v1") for i in range(100)]
    two_bad += [row(500 + i, "retrieval_empty" if i < 30 else None, "v2") for i in range(100)]
    two_bad += [row(1000 + i, "retrieval_empty" if i < 2 else None, "v3") for i in range(200)]
    fixed = version_associations(two_bad)
    assert any(a["version"] == "v3" and a["direction"] == "less_common" for a in fixed["associations"])
    assert "not proof" in out["note"]


def test_version_association_stays_quiet_without_a_real_difference_or_enough_data():
    even = [row(i, "tool_error" if i % 5 == 0 else None, "v1" if i % 2 else "v2") for i in range(400)]
    assert version_associations(even)["associations"] == []
    assert version_associations([row(i, "tool_error", "v1") for i in range(50)])["tested"] == 0   # one version only
    tiny = [row(i, "tool_error" if i < 3 else None, "v2") for i in range(10)] + [row(100 + i, None, "v1") for i in range(10)]
    assert version_associations(tiny)["associations"] == []


# ---------------------------------------------------------------- benchmark guard
def test_synthetic_benchmark_stays_above_its_measured_floors():
    result = run_benchmark(seed=0, per_cause=40)
    assert result["by_tier"]["clear"]["accuracy"] >= 0.98
    assert result["by_tier"]["compound"]["accuracy"] >= 0.98
    assert result["by_tier"]["noisy"]["accuracy"] >= 0.80   # includes expected misses (opaque timeouts, missing instrumentation)
    assert result["healthy_false_positive_rate"] == 0.0
    assert result["accuracy"] >= 0.94
