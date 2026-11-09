"""Pure tests for failure-to-test conversion: fingerprints, de-duplication, expectations, grading."""
from agent_auditor.evals import RunView, StepView, get_scorers
from agent_auditor.regress import (
    FAIL, MISSING, PASS, UNVERIFIED, case_name, derive_expectations, failure_info, fingerprint, grade_case,
    input_tokens, is_near_duplicate, plan_cases, redact_value, summarize_replay, validate_expectations,
)


def retrieval(n_docs, top=0.8):
    return StepView("retrieval", "kb", output=["d"] * n_docs, attributes={"num_docs": n_docs, "scores": [top] if n_docs else []})


def run(output="ok answer text here", steps=(), status="ok", error=None, latency=100.0):
    return RunView("r1", status=status, error=error, input="q", output=output, latency_ms=latency, steps=tuple(steps))


# ---------------------------------------------------------------- fingerprint and similarity
def test_fingerprint_ignores_case_and_whitespace_but_not_content():
    assert fingerprint("How do I  get a REFUND?") == fingerprint(" how do i get a refund? ")
    assert fingerprint("refund order") != fingerprint("refund orders")
    assert fingerprint({"q": "A", "k": 1}) == fingerprint({"k": 1, "q": "a"})


def test_near_duplicate_needs_strong_overlap_and_short_inputs_need_equality():
    a = input_tokens("How do I get a refund for my payment?")
    b = input_tokens("how can I get a refund for my payment")
    c = input_tokens("cancel my subscription plan")
    assert is_near_duplicate(a, b, 0.8)
    assert not is_near_duplicate(a, c, 0.8)
    assert not is_near_duplicate(input_tokens("refund"), input_tokens("refund policy"), 0.8)  # too short to fuzz


def test_plan_cases_merges_exact_and_near_duplicates_and_pool_grows():
    def cand(key, text, scorers=("retrieval_nonempty",)):
        return {"key": key, "fingerprint": fingerprint(text), "tokens": input_tokens(text), "scorers": frozenset(scorers)}

    known = [cand("old", "How do I get a refund for my payment?")]
    cands = [
        cand("c1", "how do i get a refund for my payment?"),          # exact (normalised) match of known
        cand("c2", "How can I get a refund for my payment"),           # near-duplicate of known
        cand("c3", "Why was my account locked after login attempts"),  # genuinely new
        cand("c4", "why was my account locked after login attempts?"), # duplicate of c3 (pool grows)
        cand("c5", "How can I get a refund for my payment", ("run_succeeded",)),  # different failure => new
    ]
    new, merged = plan_cases(cands, known, 0.8)
    assert [c["key"] for c in new] == ["c3", "c5"]
    assert {(c["key"], k) for c, k in merged} == {("c1", "old"), ("c2", "old"), ("c4", "c3")}


# ---------------------------------------------------------------- failure info and names
def test_failure_info_and_case_name():
    r = run(status="error", error="TimeoutError: tool took too long", steps=[StepView("tool", "pay_api", status="error")])
    info = failure_info(r, [{"scorer": "run_succeeded", "reason": "run failed"}, {"scorer": "no_step_errors", "reason": "1 of 1"}])
    assert info["scorers"] == ["no_step_errors", "run_succeeded"]
    assert info["error_type"] == "TimeoutError" and info["failed_steps"] == ["pay_api"]
    name = case_name(info, "How do I get a refund?")
    assert name.startswith("no_step_errors+run_succeeded: How do I get a refund?")


# ---------------------------------------------------------------- expectations
def test_derive_expectations_copies_the_original_thresholds_and_skips_weak_scorers():
    specs = get_scorers()
    failed = [
        {"scorer": "retrieval_top_score", "reason": "low", "details": {"threshold": 0.7, "top_scores": {"kb": 0.4}}},
        {"scorer": "latency_within_budget", "reason": "slow", "details": {"budget_ms": 2000, "latency_ms": 5000}},
        {"scorer": "grounded_overlap", "reason": "weak", "details": {"threshold": 0.5}},
    ]
    exp = derive_expectations(failed, specs)
    assert [e["scorer"] for e in exp] == ["retrieval_top_score", "latency_within_budget"]
    assert exp[0]["params"] == {"threshold": 0.7} and exp[1]["params"] == {"budget_ms": 2000}
    assert [e["scorer"] for e in derive_expectations(failed, specs, include_weak=True)][-1] == "grounded_overlap"


def test_validate_expectations_accepts_good_input_and_rejects_bad():
    ok = validate_expectations([{"type": "scorer", "scorer": "output_present"}, {"type": "output_contains", "value": "refund"}],
                               list(get_scorers()))
    assert ok[0]["params"] == {} and ok[1]["value"] == "refund"
    for bad in ([], "x", [{"type": "nope"}], [{"type": "scorer", "scorer": "ghost"}], [{"type": "output_contains", "value": " "}],
                [{"type": "scorer", "scorer": "output_present", "params": {"x": [1]}}]):
        try:
            validate_expectations(bad, list(get_scorers()))
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


# ---------------------------------------------------------------- grading
EXP = [
    {"type": "scorer", "scorer": "retrieval_nonempty", "params": {"min_docs": 1}},
    {"type": "scorer", "scorer": "run_succeeded", "params": {}},
]


def test_grade_pass_fail_and_unverified():
    assert grade_case(EXP, run(steps=[retrieval(3)])).outcome == PASS
    bad = grade_case(EXP, run(steps=[retrieval(0)]))
    assert bad.outcome == FAIL and [c.status for c in bad.checks] == ["fail", "pass"]
    # The agent stopped retrieving at all: the original condition can no longer be checked.
    gap = grade_case(EXP, run(steps=[]))
    assert gap.outcome == UNVERIFIED and gap.checks[0].status == "not_applicable"


def test_a_failing_check_wins_over_unverified_and_scorer_errors_are_not_failures():
    exp = EXP + [{"type": "scorer", "scorer": "ghost_scorer"}]
    assert grade_case(exp, run(steps=[retrieval(3)])).outcome == UNVERIFIED  # unknown scorer is not the agent's fault
    assert grade_case(exp, run(steps=[retrieval(0)])).outcome == FAIL


def test_output_text_checks_are_case_insensitive_and_look_inside_nested_output():
    exp = [{"type": "output_contains", "value": "Refund"}, {"type": "output_not_contains", "value": "cannot help"}]
    assert grade_case(exp, run(output={"answer": "You can request a refund in settings."})).outcome == PASS
    assert grade_case(exp, run(output="I cannot help with that")).outcome == FAIL
    assert grade_case(exp, run(output=None)).outcome == FAIL  # nothing to contain "refund"


def test_summary_separates_fixed_regressed_and_still_failing():
    outcomes = {"a": PASS, "b": PASS, "c": FAIL, "d": FAIL, "e": UNVERIFIED}
    previous = {"a": None, "b": PASS, "c": PASS, "d": FAIL, "e": FAIL}
    s = summarize_replay(outcomes, previous)
    assert s["verdict"] == "fail" and s["failed"] == 2 and s["passed"] == 2 and s["unverified"] == 1
    assert s["fixed"] == ["a"]            # b was already passing: not newly fixed
    assert s["newly_failing"] == ["c"] and s["still_failing"] == ["d"]


def test_summary_verdicts_pass_incomplete_and_empty():
    assert summarize_replay({"a": PASS, "b": PASS}, {})["verdict"] == "pass"
    assert summarize_replay({"a": PASS, "b": MISSING}, {})["verdict"] == "incomplete"
    assert summarize_replay({"a": PASS, "b": UNVERIFIED}, {})["verdict"] == "incomplete"
    assert summarize_replay({"a": PASS, "b": FAIL, "c": MISSING}, {})["verdict"] == "fail"
    assert summarize_replay({}, {})["pass_rate"] is None


# ---------------------------------------------------------------- redaction
def test_redaction_masks_common_pii_and_leaves_ordinary_text_alone():
    value = {"q": "Refund for jane.doe@example.com, card 4111 1111 1111 1111, call +1 415-555-0132 or (415) 555 0132",
             "ssn": "123-45-6789", "key": "use sk-abcdefghijklmnop1234 now", "date": "on 2026-09-30 order 12345"}
    out = redact_value(value)
    assert "[EMAIL]" in out["q"] and "[CARD]" in out["q"] and out["q"].count("[PHONE]") == 2
    assert out["ssn"] == "[SSN]" and "[SECRET]" in out["key"]
    assert out["date"] == "on 2026-09-30 order 12345"
    assert fingerprint(redact_value("mail a@x.com")) == fingerprint(redact_value("mail b@y.org"))
