from agent_auditor.evals import RunView, StepView, evaluate_run, make_judge_scorer, register_scorer
from agent_auditor.evals.scorers import _REGISTRY


def retr(name="kb", output=None, **attrs):
    return StepView("retrieval", name, output=output, attributes=attrs)


def run(**kw):
    steps = tuple(kw.pop("steps", ()))
    return RunView(id="r", steps=steps, **kw)


def score(name, view, params=None):
    results, errors = evaluate_run(view, [name], {name: params} if params else None)
    assert not errors, errors
    return results[name]


def test_run_succeeded():
    assert score("run_succeeded", run()).passed
    bad = score("run_succeeded", run(status="error", error="TimeoutError: slow"))
    assert not bad.passed and "TimeoutError" in bad.reason


def test_no_step_errors_partial_score():
    steps = [StepView("tool", "a"), StepView("tool", "b", status="error"), StepView("llm", "c"), StepView("llm", "d")]
    r = score("no_step_errors", run(steps=steps))
    assert not r.passed and r.score == 0.75 and "b" in r.reason
    assert score("no_step_errors", run()) is None  # no steps -> not applicable


def test_output_present():
    assert score("output_present", run(output="hi")).passed
    for empty in (None, "", "  ", [], {}):
        assert not score("output_present", run(output=empty)).passed


def test_retrieval_nonempty_uses_num_docs_then_output_length():
    assert not score("retrieval_nonempty", run(steps=[retr(output=[])])).passed
    assert score("retrieval_nonempty", run(steps=[retr(output=["a"])])).passed
    assert score("retrieval_nonempty", run(steps=[retr(output=None, num_docs=3)])).passed
    assert not score("retrieval_nonempty", run(steps=[retr(output=None, num_docs=0)])).passed
    assert score("retrieval_nonempty", run(steps=[retr(output=None)])) is None  # unknown count
    assert score("retrieval_nonempty", run(steps=[StepView("tool", "t")])) is None


def test_retrieval_nonempty_handles_truncated_output_marker():
    out = list(range(50)) + ["...(+25 more)"]
    r = score("retrieval_nonempty", run(steps=[retr(output=out)]))
    assert r.passed and r.details["counts"]["kb"] == 75


def test_retrieval_nonempty_min_docs_param_and_partial_score():
    steps = [retr("a", output=["x"]), retr("b", output=["x", "y", "z"])]
    r = score("retrieval_nonempty", run(steps=steps), {"min_docs": 2})
    assert not r.passed and r.score == 0.5 and "a" in r.reason


def test_retrieval_top_score_threshold():
    ok = score("retrieval_top_score", run(steps=[retr(scores=[0.9, 0.2])]))
    assert ok.passed and ok.score == 0.9
    low = score("retrieval_top_score", run(steps=[retr(scores=[0.4, 0.3])]))
    assert not low.passed
    assert score("retrieval_top_score", run(steps=[retr(scores=[0.4])]), {"threshold": 0.3}).passed
    assert score("retrieval_top_score", run(steps=[retr()])) is None


def test_latency_budget():
    assert score("latency_within_budget", run(latency_ms=500)).passed
    slow = score("latency_within_budget", run(latency_ms=20_000))
    assert not slow.passed and slow.score == 0.5
    assert score("latency_within_budget", run()) is None
    assert score("latency_within_budget", run(latency_ms=900), {"budget_ms": 1000}).passed


def test_grounded_overlap():
    docs = [{"content": "Reset your password from the account settings page using the reset link"}]
    good = run(output="Reset your password using the reset link in account settings", steps=[retr(output=docs)])
    assert score("grounded_overlap", good).passed
    made_up = run(output="Quantum entanglement powers our billing engine overnight", steps=[retr(output=docs)])
    assert not score("grounded_overlap", made_up).passed
    assert score("grounded_overlap", run(output="Reset your password using the reset link")) is None  # no context
    assert score("grounded_overlap", run(output="ok", steps=[retr(output=docs)])) is None  # too short
    dict_out = run(output={"answer": "Reset your password using the reset link in account settings"}, steps=[retr(output=docs)])
    assert score("grounded_overlap", dict_out).passed


def test_scorer_exception_is_reported_not_raised():
    results, errors = evaluate_run(run(latency_ms=5), ["latency_within_budget"], {"latency_within_budget": {"budget_ms": "fast"}})
    assert results == {} and "budget_ms" in errors["latency_within_budget"]


def test_unknown_scorer_reported():
    results, errors = evaluate_run(run(), ["nope"])
    assert results == {} and errors == {"nope": "unknown scorer"}


def test_default_set_excludes_judges_and_includes_builtins():
    results, errors = evaluate_run(run(output="x", latency_ms=1))
    assert not errors
    assert {"run_succeeded", "output_present", "latency_within_budget"} <= set(results)
    assert results["retrieval_nonempty"] is None  # not applicable, still reported as None


def test_custom_judge_scorer():
    spec = make_judge_scorer("test_judge_a", lambda v: (0.8, "looks fine"), threshold=0.7)
    register_scorer(spec)
    try:
        r = score("test_judge_a", run())
        assert r.passed and r.score == 0.8 and r.reason == "looks fine"
        assert not score("test_judge_a", run(), {"threshold": 0.9}).passed
        results, _ = evaluate_run(run())
        assert "test_judge_a" not in results  # judges are opt-in
    finally:
        _REGISTRY.pop("test_judge_a", None)


def test_judge_out_of_range_is_an_error():
    register_scorer(make_judge_scorer("test_judge_b", lambda v: 1.5))
    try:
        _, errors = evaluate_run(run(), ["test_judge_b"])
        assert "between 0 and 1" in errors["test_judge_b"]
    finally:
        _REGISTRY.pop("test_judge_b", None)


def test_duplicate_registration_rejected():
    spec = make_judge_scorer("test_judge_c", lambda v: 1.0)
    register_scorer(spec)
    try:
        try:
            register_scorer(spec)
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")
        register_scorer(spec, replace=True)
    finally:
        _REGISTRY.pop("test_judge_c", None)
