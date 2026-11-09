"""Release gate and state machine (pure: no database)."""
from datetime import datetime, timedelta, timezone

from agent_auditor.release import (
    Conflict, GatePolicy, InvalidTransition, check_action, evaluate_gate, recommend,
)

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def report(hours_ago=1.0, verdict="promote", baseline="v1", rid="r"):
    return {"id": rid, "created_at": NOW - timedelta(hours=hours_ago), "verdict": verdict, "baseline": baseline}


def approval(who, hours_ago=0.5, decision="approve"):
    return {"approver": who, "decision": decision, "evidence_at": NOW - timedelta(hours=hours_ago + 0.1)}


def gate(reports=None, approvals=(), policy=None, active="v1", replay=None, proposer="alice"):
    return evaluate_gate(
        policy=policy or GatePolicy(), now=NOW, proposer=proposer, active_label=active,
        canary_reports=[report()] if reports is None else reports, replay=replay, approvals=list(approvals),
    )


def failed(result):
    return {c.name for c in result.checks if not c.passed}


def test_ready_with_good_evidence_and_one_approval():
    g = gate(approvals=[approval("bob")])
    assert g.ready and g.evidence_ok and g.valid_approvers == ["bob"] and g.blockers == []


def test_no_canary_report_blocks():
    g = gate(reports=[])
    assert not g.evidence_ok and "canary_evidence" in failed(g)


def test_non_promote_verdicts_block():
    for verdict in ("hold", "rollback", "continue"):
        g = gate(reports=[report(verdict=verdict)], approvals=[approval("bob")])
        assert not g.ready and "canary_verdict" in failed(g)


def test_old_evidence_blocks():
    g = gate(reports=[report(hours_ago=30)], approvals=[approval("bob", hours_ago=29)])
    assert "canary_fresh" in failed(g) and not g.ready


def test_baseline_must_be_the_active_version():
    g = gate(reports=[report(baseline="v0")], approvals=[approval("bob")])
    assert "canary_baseline" in failed(g)
    assert gate(reports=[report(baseline="v0")], approvals=[approval("bob")],
                policy=GatePolicy(require_baseline_is_active=False)).ready
    assert "canary_baseline" in failed(gate(active=None, approvals=[approval("bob")]))


def test_proposer_cannot_approve_own_release_by_default():
    g = gate(approvals=[approval("alice")])
    assert not g.ready and g.valid_approvers == []
    assert gate(approvals=[approval("alice")], policy=GatePolicy(allow_self_approval=True)).ready


def test_needs_distinct_approvers():
    two = GatePolicy(required_approvals=2)
    assert not gate(approvals=[approval("bob"), approval("bob")], policy=two).ready
    assert gate(approvals=[approval("bob"), approval("carol")], policy=two).ready


def test_reject_decisions_do_not_count_as_approval():
    assert not gate(approvals=[approval("bob", decision="reject")]).ready


def test_newer_bad_report_makes_approval_stale():
    reports = [report(hours_ago=2, rid="a"), report(hours_ago=0.2, verdict="hold", rid="b")]
    g = gate(reports=reports, approvals=[approval("bob", hours_ago=1.0)])
    assert g.stale_approvers == ["bob"] and not g.ready


def test_newer_good_report_keeps_approval_valid():
    reports = [report(hours_ago=2, rid="a"), report(hours_ago=0.2, rid="b")]
    assert gate(reports=reports, approvals=[approval("bob", hours_ago=1.0)]).ready


def test_replay_requirement():
    policy = GatePolicy(require_replay=True)
    assert "replay" in failed(gate(approvals=[approval("bob")], policy=policy))
    bad = {"verdict": "fail", "summary": {"newly_failing": ["c1"]}}
    assert "replay" in failed(gate(approvals=[approval("bob")], policy=policy, replay=bad))
    good = {"verdict": "pass", "summary": {"newly_failing": []}}
    assert gate(approvals=[approval("bob")], policy=policy, replay=good).ready


def test_naive_datetimes_are_treated_as_utc():
    naive = {"id": "r", "created_at": (NOW - timedelta(hours=1)).replace(tzinfo=None), "verdict": "promote", "baseline": "v1"}
    assert gate(reports=[naive], approvals=[approval("bob")]).ready


def test_transitions():
    check_action("proposed", "promote")
    check_action("promoted", "rollback")
    for state, action in (("promoted", "promote"), ("rolled_back", "rollback"), ("proposed", "rollback"),
                          ("rejected", "approve"), ("cancelled", "promote")):
        try:
            check_action(state, action)
        except InvalidTransition as exc:
            assert isinstance(exc, Conflict)
            continue
        raise AssertionError(f"{action} from {state} should be refused")
    try:
        check_action("proposed", "explode")
    except ValueError:
        return
    raise AssertionError("unknown action should be a ValueError")


def test_policy_validation():
    for bad in (GatePolicy(required_approvals=0), GatePolicy(max_evidence_age_hours=0)):
        try:
            bad.validate()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")


def test_recommendations():
    ready = gate(approvals=[approval("bob")])
    assert recommend("proposed", ready) == "ready to promote"
    assert recommend("proposed", gate(reports=[])).startswith("blocked on evidence")
    assert "waiting for approval" in recommend("proposed", gate())
    assert "rollback recommended" in recommend("promoted", None, "rollback")
    assert recommend("promoted", None, "promote") == "promoted; keep monitoring"
    assert recommend("rejected", None).startswith("release is rejected")
