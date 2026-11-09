import math
import random

from agent_auditor.drift.stats import (
    js_divergence, ks_two_sample, permutation_js_test, psi, quantile, summarize,
)


def raises(exc, fn, *args):
    try:
        fn(*args)
    except exc:
        return True
    return False


def test_quantile_and_summarize():
    assert quantile([1, 2, 3, 4, 5], 0.5) == 3
    assert quantile([1, 2, 3, 4], 0.5) == 2.5
    assert quantile([7], 0.9) == 7.0
    assert raises(ValueError, quantile, [], 0.5)
    s = summarize([1, 2, 3, 4, 100])
    assert s["n"] == 5 and s["median"] == 3 and s["max"] == 100 and s["mean"] == 22
    assert summarize([]) == {"n": 0}


def test_ks_identical_and_disjoint():
    d, p = ks_two_sample(list(range(50)), list(range(50)))
    assert d == 0.0 and p == 1.0
    d, p = ks_two_sample(list(range(100)), list(range(1000, 1100)))
    assert d == 1.0 and p < 1e-20


def test_ks_known_case_matches_exact_test_closely():
    # SciPy's exact test gives D=0.5, p=0.00096... for these samples; the asymptotic formula is close.
    d, p = ks_two_sample(list(range(30)), list(range(15, 45)))
    assert d == 0.5 and 0.0004 < p < 0.003


def test_ks_handles_ties():
    cycle = [800, 900, 1000, 1100, 900]
    d, p = ks_two_sample(cycle * 18, cycle * 8)
    assert d == 0.0 and p == 1.0
    d, p = ks_two_sample(cycle * 18, [x + 1000 for x in cycle * 8])
    assert d == 1.0 and p < 1e-10


def test_ks_requires_data():
    assert raises(ValueError, ks_two_sample, [], [1.0])
    assert raises(ValueError, ks_two_sample, [1.0], [])


def test_psi_constant_and_discrete_references():
    assert psi([2.0] * 50, [2.0] * 50) == 0.0
    assert psi([2.0] * 50, [3.0] * 50) > 5  # a move to never-seen values is a big shift
    cycle = [800, 900, 1000, 1100, 900]
    assert psi(cycle * 18, cycle * 8) < 0.01
    assert psi(cycle * 18, [x + 1000 for x in cycle * 8]) > 1


def test_psi_grows_with_shift_and_is_small_when_nothing_changed():
    r = random.Random(5)
    ref = [r.gauss(0, 1) for _ in range(1000)]
    same = [r.gauss(0, 1) for _ in range(300)]
    shifted = [r.gauss(1, 1) for _ in range(300)]
    assert psi(ref, same) < 0.12 < 0.5 < psi(ref, shifted)
    assert raises(ValueError, psi, [], [1.0])


def test_js_divergence_properties():
    a = {"x": 5, "y": 5}
    assert js_divergence(a, a) == 0.0
    assert math.isclose(js_divergence({"x": 1}, {"y": 1}), 1.0)
    a, b = {"x": 3, "y": 1}, {"x": 1, "y": 3}
    assert math.isclose(js_divergence(a, b), js_divergence(b, a))
    assert 0 < js_divergence(a, b) < 1
    assert js_divergence({}, {}) == 0.0 and js_divergence({}, {"x": 1}) == 1.0


TOPICS = [["reset", "password"], ["change", "email"], ["cancel", "plan"], ["invoice", "copy"]]


def test_permutation_test_same_vs_shifted_and_deterministic():
    ref = [TOPICS[i % 4] for i in range(120)]
    same = [TOPICS[i % 4] for i in range(60)]
    shifted = [["refund", "payment"] if i % 3 else TOPICS[i % 4] for i in range(60)]
    js_same, p_same = permutation_js_test(ref, same, 500, random.Random(1))
    js_shift, p_shift = permutation_js_test(ref, shifted, 500, random.Random(1))
    assert js_same < 0.01 and p_same > 0.2
    assert js_shift > 0.3 and math.isclose(p_shift, 1 / 501)
    assert permutation_js_test(ref, shifted, 500, random.Random(1)) == (js_shift, p_shift)


def test_permutation_test_degenerate_inputs():
    js, p = permutation_js_test([["a"]] * 5, [], 50, random.Random(0))
    assert p == 1.0
