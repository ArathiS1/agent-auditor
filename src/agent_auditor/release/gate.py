"""The release gate and the release state machine. Pure: plain data in, plain data out.

A release moves a candidate version to "active" (the version production should run).

States      proposed -> promoted -> rolled_back          (promoted and rolled_back are the happy / undo path)
            proposed -> rejected | cancelled              (terminal)

What the gate checks before a promotion (all must pass)
  evidence   the newest canary report for the candidate says "promote", is recent, and compared the candidate
             with the version that is active right now (evidence about some other baseline proves nothing)
  replay     optional: the newest regression replay of the candidate has no newly failing case
  approvals  enough distinct approvers (not the proposer, unless allowed) whose approval is still valid

An approval goes stale when a newer canary report for the same candidate says anything other than "promote".
That is the point of the gate: a person approved on evidence, and the evidence has since changed.

Identity caveat: approver names are whatever the caller sends. With one shared API key this is a labelled
audit trail, not authentication. Do not treat "two different approvers" as two different people until the API
sits behind real user authentication.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

ACTIONS = {
    "approve": {"proposed"}, "reject": {"proposed"}, "cancel": {"proposed"},
    "promote": {"proposed"}, "rollback": {"promoted"},
}


class Conflict(Exception):
    """The request is valid but conflicts with the current state."""


class InvalidTransition(Conflict):
    pass


def check_action(state: str, action: str) -> None:
    allowed = ACTIONS.get(action)
    if allowed is None:
        raise ValueError(f"unknown action '{action}'")
    if state not in allowed:
        raise InvalidTransition(f"cannot {action} a release that is {state} (allowed from: {', '.join(sorted(allowed))})")


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@dataclass(frozen=True)
class GatePolicy:
    required_approvals: int = 1
    allow_self_approval: bool = False
    max_evidence_age_hours: float = 24.0
    require_replay: bool = False
    require_baseline_is_active: bool = True

    def validate(self) -> None:
        if not 1 <= self.required_approvals <= 10:
            raise ValueError("required_approvals must be between 1 and 10")
        if self.max_evidence_age_hours <= 0:
            raise ValueError("max_evidence_age_hours must be positive")


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class GateResult:
    evidence_ok: bool  # everything except approvals
    ready: bool  # evidence_ok and enough valid approvals
    checks: list[GateCheck]
    valid_approvers: list[str]
    stale_approvers: list[str]
    blockers: list[str] = field(default_factory=list)


def evaluate_gate(
    *,
    policy: GatePolicy,
    now: datetime,
    proposer: str,
    active_label: str | None,
    canary_reports: Sequence[Mapping[str, Any]],
    replay: Mapping[str, Any] | None,
    approvals: Sequence[Mapping[str, Any]],
) -> GateResult:
    """canary_reports: every saved report for the candidate ({id, created_at, verdict, baseline}).
    approvals: {approver, decision, evidence_at}; only decision == "approve" counts."""
    now = as_utc(now)
    checks: list[GateCheck] = []
    reports = sorted(canary_reports, key=lambda r: as_utc(r["created_at"]))
    latest = reports[-1] if reports else None

    if latest is None:
        checks.append(GateCheck("canary_evidence", False, "no canary report for this candidate; run a canary evaluation"))
    else:
        verdict_ok = latest["verdict"] == "promote"
        checks.append(GateCheck(
            "canary_verdict", verdict_ok,
            "newest canary report says promote" if verdict_ok else f"newest canary report says {latest['verdict']}, not promote",
        ))
        age = now - as_utc(latest["created_at"])
        fresh = age <= timedelta(hours=policy.max_evidence_age_hours)
        checks.append(GateCheck(
            "canary_fresh", fresh,
            f"canary report is {age.total_seconds() / 3600:.1f} h old" + ("" if fresh else f" (limit {policy.max_evidence_age_hours:g} h); re-run it"),
        ))
        if policy.require_baseline_is_active:
            same = active_label is not None and latest["baseline"] == active_label
            checks.append(GateCheck(
                "canary_baseline", same,
                f"canary baseline '{latest['baseline']}' is the active version" if same
                else f"canary compared against '{latest['baseline']}' but the active version is '{active_label}'; re-run against the active version",
            ))

    if policy.require_replay:
        if replay is None:
            checks.append(GateCheck("replay", False, "policy requires a regression replay of the candidate and none exists"))
        else:
            newly = list((replay.get("summary") or {}).get("newly_failing") or [])
            ok = not newly and replay.get("verdict") == "pass"
            checks.append(GateCheck(
                "replay", ok,
                "regression replay passed" if ok else f"regression replay verdict {replay.get('verdict')}, {len(newly)} newly failing case(s)",
            ))

    evidence_ok = all(c.passed for c in checks)

    valid: list[str] = []
    stale: list[str] = []
    for a in approvals:
        if a.get("decision") != "approve" or a["approver"] in valid or a["approver"] in stale:
            continue
        if a["approver"] == proposer and not policy.allow_self_approval:
            continue
        at = a.get("evidence_at")
        superseded = at is None or any(as_utc(r["created_at"]) > as_utc(at) and r["verdict"] != "promote" for r in reports)
        (stale if superseded else valid).append(a["approver"])
    approvals_ok = len(valid) >= policy.required_approvals
    detail = f"{len(valid)} of {policy.required_approvals} required approval(s)"
    if stale:
        detail += f"; stale (evidence changed since): {', '.join(stale)}"
    checks.append(GateCheck("approvals", approvals_ok, detail))

    blockers = [f"{c.name}: {c.reason}" for c in checks if not c.passed]
    return GateResult(evidence_ok, evidence_ok and approvals_ok, checks, valid, stale, blockers)


def recommend(state: str, gate: GateResult | None, post_promotion_verdict: str | None = None) -> str:
    """One line telling a person what to do next."""
    if state == "proposed":
        if gate is None:
            return "evaluate the gate"
        if gate.ready:
            return "ready to promote"
        if not gate.evidence_ok:
            return "blocked on evidence: " + "; ".join(b for b in gate.blockers if not b.startswith("approvals"))
        return "evidence is good; waiting for approval(s)"
    if state == "promoted":
        if post_promotion_verdict == "rollback":
            return "rollback recommended: a canary evaluation since promotion says rollback"
        if post_promotion_verdict == "hold":
            return "watch closely: a canary evaluation since promotion says hold"
        return "promoted; keep monitoring"
    return f"release is {state}; nothing to do"
