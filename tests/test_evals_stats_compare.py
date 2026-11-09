import math

from agent_auditor.evals import Counts, compare_counts, fisher_exact_two_sided, wilson_interval


def test_wilson_basic_properties():
    lo, hi = wilson_interval(50, 100)
    assert 0.40 < lo < 0.5 < hi < 0.60
    assert wilson_interval(0, 0) == (0.0, 1.0)
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0 and 0.2 < hi < 0.4
    lo, hi = wilson_interval(10, 10)
    assert hi == 1.0 and 0.6 < lo < 0.8
    assert wilson_interval(5, 10)[1] - wilson_interval(5, 10)[0] > wilson_interval(500, 1000)[1] - wilson_interval(500, 1000)[0]


def test_fisher_known_values():
    # Classic tea-tasting table [[3,1],[1,3]]: two-sided p = 0.4857...
    assert math.isclose(fisher_exact_two_sided(3, 4, 1, 4), 0.4857142857, rel_tol=1e-6)
    assert math.isclose(fisher_exact_two_sided(5, 10, 5, 10), 1.0, abs_tol=1e-9)
    assert fisher_exact_two_sided(10, 10, 0, 10) < 1e-4
    assert fisher_exact_two_sided(0, 0, 3, 5) == 1.0


def test_fisher_rejects_impossible_counts():
    try:
        fisher_exact_two_sided(11, 10, 1, 10)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_clear_regression_detected():
    cmp = compare_counts({"retrieval_nonempty": Counts(100, 95)}, {"retrieval_nonempty": Counts(100, 70)})
    assert cmp.overall_verdict == "regression"
    row = cmp.scorers[0]
    assert row.verdict == "regression" and row.delta == -0.25 and row.p_value < 0.001
    assert row.baseline_ci[0] < 0.95 < row.baseline_ci[1]


def test_clear_improvement_detected():
    cmp = compare_counts({"s": Counts(100, 60)}, {"s": Counts(100, 90)})
    assert cmp.overall_verdict == "improvement"


def test_small_drop_is_no_change_and_big_but_noisy_drop_is_inconclusive():
    assert compare_counts({"s": Counts(200, 190)}, {"s": Counts(200, 188)}).overall_verdict == "no_change"
    noisy = compare_counts({"s": Counts(20, 18)}, {"s": Counts(20, 15)})  # -15 points, but only 20 runs each
    assert noisy.scorers[0].verdict == "inconclusive" and noisy.overall_verdict == "inconclusive"


def test_insufficient_data():
    cmp = compare_counts({"s": Counts(5, 5)}, {"s": Counts(100, 50)})
    assert cmp.overall_verdict == "insufficient_data"
    assert cmp.scorers[0].p_value is None and "at least 20" in cmp.scorers[0].reason
    assert compare_counts({}, {}).overall_verdict == "insufficient_data"


def test_missing_scorer_on_one_side_is_insufficient_not_an_error():
    cmp = compare_counts({"a": Counts(50, 50), "b": Counts(50, 50)}, {"a": Counts(50, 50)})
    verdicts = {r.scorer: r.verdict for r in cmp.scorers}
    assert verdicts == {"a": "no_change", "b": "insufficient_data"}
    assert cmp.overall_verdict == "no_change" and any("not compared" in n for n in cmp.notes)


def test_regression_in_one_scorer_wins_overall():
    base = {"a": Counts(100, 95), "b": Counts(100, 95)}
    cand = {"a": Counts(100, 95), "b": Counts(100, 60)}
    cmp = compare_counts(base, cand)
    assert cmp.overall_verdict == "regression"
    assert {r.scorer: r.verdict for r in cmp.scorers} == {"a": "no_change", "b": "regression"}


def test_bonferroni_makes_borderline_result_not_significant():
    base = {k: Counts(60, 54) for k in "abcdef"}
    cand = {k: Counts(60, 54) for k in "abcdef"}
    cand["a"] = Counts(60, 44)  # p ~ 0.04: significant alone, not after splitting alpha six ways
    corrected = compare_counts(base, cand)
    uncorrected = compare_counts(base, cand, correct_multiple=False)
    assert uncorrected.overall_verdict == "regression"
    assert corrected.overall_verdict == "inconclusive"
    assert math.isclose(corrected.scorers[0].alpha_used, 0.05 / 6)


def test_parameters_validated():
    for kwargs in ({"min_samples": 0}, {"alpha": 0}, {"alpha": 1}, {"min_effect": -0.1}):
        try:
            compare_counts({}, {}, **kwargs)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {kwargs}")
