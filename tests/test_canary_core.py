"""Canary checks and the decision rules (pure: no database)."""
from __future__ import annotations

import random

from agent_auditor.canary import Policy, check_causes, check_drift, check_evals, check_latency, check_replay, decide
from agent_auditor.canary.checks import CheckResult
from agent_auditor.drift.detect import DriftReport
from agent_auditor.evals.compare import Counts

P = Policy()


def counts(n, passed):
    return {"run_succeeded": Counts(n, passed, passed / n)}


def rows(n, causes):
    """n runs; `causes` maps cause -> number of failed runs with that cause."""
    out = [{"status": "failed", "cause": c} for c, k in causes.items() for _ in range(k)]
    return out + [{"status": "healthy", "cause": None}] * (n - len(out))


# ---- evals
def test_evals_pass_when_equal():
    assert check_evals(counts(100, 90), counts(100, 90), P).status == "pass"


def test_evals_big_regression_is_major():
    r = check_evals(counts(200, 180), counts(200, 140), P)
    assert (r.status, r.severity) == ("fail", "major")


def test_evals_small_significant_regression_is_minor():
    r = check_evals(counts(2000, 1900), counts(2000, 1780), P)  # -6 points, very significant
    assert (r.status, r.severity) == ("fail", "minor")


def test_evals_inconclusive_warns():
    r = check_evals(counts(30, 27), counts(30, 22), P)
    assert r.status == "warn"


def test_evals_too_few_samples_skipped():
    assert check_evals(counts(10, 9), counts(10, 5), P).status == "skipped"


# ---- latency
def lat(n, mean, seed):
    rng = random.Random(seed)
    return [max(1.0, rng.gauss(mean, mean * 0.1)) for _ in range(n)]


def test_latency_same_passes():
    assert check_latency(lat(60, 1000, 1), lat(60, 1000, 2), P).status == "pass"


def test_latency_warn_fail_rollback_bands():
    base = lat(80, 1000, 1)
    assert check_latency(base, lat(80, 1500, 2), P).status == "warn"
    r = check_latency(base, lat(80, 2400, 3), P)
    assert (r.status, r.severity) == ("fail", "minor")
    r = check_latency(base, lat(80, 5000, 4), P)
    assert (r.status, r.severity) == ("fail", "major")


def test_latency_needs_samples():
    assert check_latency(lat(10, 1000, 1), lat(10, 3000, 2), P).status == "skipped"


def test_latency_faster_is_fine():
    assert check_latency(lat(60, 1000, 1), lat(60, 500, 2), P).status == "pass"


# ---- causes
def test_causes_new_cause_major():
    r = check_causes(rows(100, {"llm_error": 3}), rows(100, {"llm_error": 3, "tool_timeout": 25}), P)
    assert (r.status, r.severity) == ("fail", "major")
    assert any(c["new"] for c in r.details["causes"] if c["cause"] == "tool_timeout")


def test_causes_moderate_increase_warns():
    r = check_causes(rows(400, {"retrieval_empty": 4}), rows(400, {"retrieval_empty": 36}), P)
    assert (r.status, r.severity) == ("warn", "none")


def test_causes_decrease_not_flagged():
    r = check_causes(rows(100, {"llm_error": 30}), rows(100, {"llm_error": 2}), P)
    assert r.status == "pass"


def test_causes_skipped_when_thin():
    assert check_causes(rows(10, {}), rows(10, {}), P).status == "skipped"


# ---- replay
def replay(newly=(), total=20, verdict="pass"):
    return {"id": "r1", "verdict": verdict, "summary": {"total": total, "newly_failing": list(newly)}}


def test_replay_missing_skipped_or_required():
    assert check_replay(None, P).status == "skipped"
    r = check_replay(None, Policy(require_replay=True))
    assert (r.status, r.severity) == ("fail", "minor")


def test_replay_newly_failing_bands():
    assert check_replay(replay(), P).status == "pass"
    r = check_replay(replay(["a"]), P)
    assert (r.status, r.severity) == ("fail", "minor")
    r = check_replay(replay(["a", "b"]), P)
    assert (r.status, r.severity) == ("fail", "major")


def test_replay_incomplete_warns():
    assert check_replay(replay(verdict="incomplete"), P).status == "warn"


# ---- drift
def drift_report(overall):
    return DriftReport(overall, [], 50, 50, 0.01, 30, [])


def test_drift_warns_not_fails():
    assert check_drift(drift_report("drift"), P).status == "warn"
    assert check_drift(drift_report("stable"), P).status == "pass"
    assert check_drift(drift_report("insufficient_data"), P).status == "skipped"
    assert check_drift(None, P).status == "skipped"


# ---- decide
def ok(name):
    return CheckResult(name, "pass", "none", "ok")


def test_decide_promote():
    d = decide([ok("evals"), ok("latency")], canary_runs=50, baseline_runs=50, policy=P)
    assert d.verdict == "promote"


def test_decide_major_beats_everything():
    bad = CheckResult("replay", "fail", "major", "x")
    d = decide([ok("evals"), bad], canary_runs=1, baseline_runs=1, policy=P)
    assert d.verdict == "rollback" and d.blocking == ["replay"]


def test_decide_minor_is_hold():
    bad = CheckResult("latency", "fail", "minor", "x")
    assert decide([ok("evals"), bad], canary_runs=50, baseline_runs=50, policy=P).verdict == "hold"


def test_decide_continue_when_thin():
    d = decide([ok("evals")], canary_runs=5, baseline_runs=50, policy=P)
    assert d.verdict == "continue" and "5 canary" in d.reasons[0]


def test_decide_continue_without_evals():
    skipped = CheckResult("evals", "skipped", "none", "no data")
    assert decide([skipped, ok("latency")], canary_runs=50, baseline_runs=50, policy=P).verdict == "continue"
    assert decide([ok("latency")], canary_runs=50, baseline_runs=50, policy=P).verdict == "continue"


def test_decide_warnings_block_option():
    w = CheckResult("drift", "warn", "none", "shifted")
    checks = [ok("evals"), w]
    assert decide(checks, canary_runs=50, baseline_runs=50, policy=P).verdict == "promote"
    d = decide(checks, canary_runs=50, baseline_runs=50, policy=Policy(warnings_block=True))
    assert d.verdict == "hold"


def test_policy_validation():
    for bad in (Policy(alpha=2), Policy(min_effect=0.5, rollback_effect=0.1), Policy(latency_warn_ratio=3.0)):
        try:
            bad.validate()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")
