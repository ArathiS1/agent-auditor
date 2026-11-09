"""Database-facing part of releases: propose, approve, promote, roll back, and read the audit trail."""
from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..audit import record
from ..canary.service import _latest_replay
from ..evals.service import _find_version, get_agent
from ..models import Agent, AgentVersion, AuditEvent, CanaryReport, Release, ReleaseApproval, utcnow
from .gate import Conflict, GatePolicy, GateResult, as_utc, check_action, evaluate_gate, recommend

POLICY_FIELDS = {f.name for f in fields(GatePolicy)}


def _name(value: str | None, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"{what} is required")
    if len(value) > 100:
        raise ValueError(f"{what} is too long (max 100 characters)")
    return value


def _active_label(db: Session, agent_id: str) -> str | None:
    return db.scalar(select(AgentVersion.label).where(AgentVersion.agent_id == agent_id, AgentVersion.is_active.is_(True)))


def _activate(db: Session, agent_id: str, label: str) -> None:
    db.execute(update(AgentVersion).where(AgentVersion.agent_id == agent_id).values(is_active=False))
    db.execute(update(AgentVersion).where(AgentVersion.agent_id == agent_id, AgentVersion.label == label).values(is_active=True))


def _get(db: Session, release_id: str) -> Release:
    release = db.get(Release, release_id)
    if release is None:
        raise LookupError("release not found")
    return release


def _policy_of(release: Release) -> GatePolicy:
    return GatePolicy(**{k: v for k, v in (release.policy or {}).items() if k in POLICY_FIELDS})


def _reports(db: Session, release: Release) -> list[CanaryReport]:
    return list(db.scalars(
        select(CanaryReport).where(CanaryReport.agent_id == release.agent_id, CanaryReport.candidate_label == release.candidate_label)
        .order_by(CanaryReport.created_at)
    ))


def _gate(db: Session, release: Release) -> GateResult:
    reports = _reports(db, release)
    return evaluate_gate(
        policy=_policy_of(release), now=utcnow(), proposer=release.proposed_by,
        active_label=_active_label(db, release.agent_id),
        canary_reports=[
            {"id": r.id, "created_at": r.created_at, "verdict": r.verdict, "baseline": r.baseline_label} for r in reports
        ],
        replay=_latest_replay(db, release.agent_id, release.candidate_label),
        approvals=[
            {"approver": a.approver, "decision": a.decision, "evidence_at": a.evidence_at} for a in release.approvals
        ],
    )


def _gate_dict(g: GateResult) -> dict[str, Any]:
    return {"ready": g.ready, "evidence_ok": g.evidence_ok, "checks": [asdict(c) for c in g.checks],
            "valid_approvers": g.valid_approvers, "stale_approvers": g.stale_approvers, "blockers": g.blockers}


def release_to_dict(db: Session, release: Release, agent_name: str) -> dict[str, Any]:
    gate = _gate(db, release) if release.state == "proposed" else None
    post = None
    if release.state == "promoted" and release.promoted_at is not None:
        after = [r for r in _reports(db, release) if as_utc(r.created_at) > as_utc(release.promoted_at)]
        post = after[-1].verdict if after else None
    return {
        "id": release.id, "agent": agent_name, "candidate": release.candidate_label, "baseline": release.baseline_label,
        "state": release.state, "proposed_by": release.proposed_by, "notes": release.notes,
        "policy": asdict(_policy_of(release)), "created_at": release.created_at, "updated_at": release.updated_at,
        "promoted_at": release.promoted_at, "promoted_by": release.promoted_by,
        "previous_active": release.previous_active_label, "rolled_back_at": release.rolled_back_at,
        "rolled_back_by": release.rolled_back_by, "restored": release.restored_label, "closed_reason": release.closed_reason,
        "approvals": [
            {"approver": a.approver, "decision": a.decision, "comment": a.comment, "created_at": a.created_at,
             "canary_report_id": a.canary_report_id}
            for a in release.approvals
        ],
        "gate": _gate_dict(gate) if gate else None,
        "recommendation": recommend(release.state, gate, post),
    }


def _out(db: Session, release: Release) -> dict[str, Any]:
    return release_to_dict(db, release, get_agent_name(db, release.agent_id))


def get_agent_name(db: Session, agent_id: str) -> str:
    return db.scalar(select(Agent.name).where(Agent.id == agent_id)) or ""


def _touch(release: Release) -> None:
    release.updated_at = utcnow()


# --------------------------------------------------------------------------- actions
def propose(
    db: Session, *, agent: str, candidate: str, proposed_by: str, notes: str | None = None, policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    proposer = _name(proposed_by, "proposed_by")
    unknown = set(policy or {}) - POLICY_FIELDS
    if unknown:
        raise ValueError("unknown policy field(s): " + ", ".join(sorted(unknown)))
    pol = GatePolicy(**(policy or {}))
    pol.validate()
    agent_row = get_agent(db, agent)
    _find_version(db, agent_row, candidate)
    active = _active_label(db, agent_row.id)
    if active == candidate:
        raise Conflict(f"'{candidate}' is already the active version")
    open_release = db.scalar(select(Release).where(
        Release.agent_id == agent_row.id, Release.candidate_label == candidate, Release.state == "proposed"))
    if open_release is not None:
        raise Conflict(f"release {open_release.id} for '{candidate}' is already open; approve, promote, reject or cancel it first")
    release = Release(
        agent_id=agent_row.id, candidate_label=candidate, baseline_label=active, proposed_by=proposer,
        notes=notes, policy=asdict(pol),
    )
    db.add(release)
    db.flush()
    record(db, "release.proposed", "release", release.id,
           {"agent": agent_row.name, "candidate": candidate, "baseline": active, "policy": asdict(pol)}, actor=proposer)
    return _out(db, release)


def decide(db: Session, release_id: str, *, approver: str, decision: str, comment: str | None = None) -> dict[str, Any]:
    """Record an approval or a rejection. Approving requires the evidence checks to pass right now."""
    who = _name(approver, "approver")
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")
    release = _get(db, release_id)
    check_action(release.state, decision)
    policy = _policy_of(release)
    agent_name = get_agent_name(db, release.agent_id)

    if decision == "reject":
        _upsert_approval(db, release, who, "reject", comment, None, None)
        release.state, release.closed_reason = "rejected", comment or f"rejected by {who}"
        _touch(release)
        record(db, "release.rejected", "release", release.id,
               {"agent": agent_name, "candidate": release.candidate_label, "comment": comment}, actor=who)
        db.flush()
        return _out(db, release)

    if who == release.proposed_by and not policy.allow_self_approval:
        raise PermissionError("the proposer cannot approve their own release under this policy")
    gate = _gate(db, release)
    if not gate.evidence_ok:
        record(db, "release.approval_refused", "release", release.id,
               {"agent": agent_name, "approver": who, "blockers": gate.blockers}, actor=who)
        db.flush()
        raise Conflict("cannot approve while the evidence checks fail: " + "; ".join(b for b in gate.blockers if not b.startswith("approvals")))
    latest = _reports(db, release)[-1]
    _upsert_approval(db, release, who, "approve", comment, latest.id, latest.created_at)
    _touch(release)
    record(db, "release.approved", "release", release.id,
           {"agent": agent_name, "candidate": release.candidate_label, "canary_report_id": latest.id, "comment": comment}, actor=who)
    db.flush()
    return _out(db, release)


def _upsert_approval(db: Session, release: Release, who: str, decision: str, comment, report_id, evidence_at) -> None:
    existing = next((a for a in release.approvals if a.approver == who), None)
    if existing is None:
        release.approvals.append(ReleaseApproval(
            approver=who, decision=decision, comment=comment, canary_report_id=report_id, evidence_at=evidence_at))
    else:
        existing.decision, existing.comment, existing.canary_report_id = decision, comment, report_id
        existing.evidence_at, existing.created_at = evidence_at, utcnow()


def promote(db: Session, release_id: str, *, actor: str) -> dict[str, Any]:
    """Activate the candidate if the gate passes right now. Returns {"promoted": bool, "release": ..., "blockers": [...]}.
    A refusal is audited, so the caller should commit either way."""
    who = _name(actor, "actor")
    release = _get(db, release_id)
    check_action(release.state, "promote")
    agent_name = get_agent_name(db, release.agent_id)
    gate = _gate(db, release)
    if not gate.ready:
        record(db, "release.promote_refused", "release", release.id,
               {"agent": agent_name, "candidate": release.candidate_label, "blockers": gate.blockers}, actor=who)
        db.flush()
        return {"promoted": False, "release": _out(db, release), "blockers": gate.blockers}
    previous = _active_label(db, release.agent_id)
    _activate(db, release.agent_id, release.candidate_label)
    now = utcnow()
    release.state, release.promoted_at, release.promoted_by, release.previous_active_label = "promoted", now, who, previous
    _touch(release)
    record(db, "release.promoted", "release", release.id,
           {"agent": agent_name, "candidate": release.candidate_label, "previous_active": previous,
            "approvers": gate.valid_approvers}, actor=who)
    db.flush()
    return {"promoted": True, "release": _out(db, release), "blockers": []}


def rollback(db: Session, release_id: str, *, actor: str, reason: str, to_version: str | None = None) -> dict[str, Any]:
    who = _name(actor, "actor")
    why = _name(reason, "reason")
    release = _get(db, release_id)
    check_action(release.state, "rollback")
    agent_row = get_agent(db, get_agent_name(db, release.agent_id))
    active = _active_label(db, release.agent_id)
    if active != release.candidate_label:
        raise Conflict(f"'{release.candidate_label}' is not the active version any more (active: {active}); "
                       "only the currently active release can be rolled back")
    target = to_version or release.previous_active_label
    if target is None:
        raise ValueError("no previous active version was recorded for this release; pass to_version")
    _find_version(db, agent_row, target)
    _activate(db, release.agent_id, target)
    release.state, release.rolled_back_at, release.rolled_back_by = "rolled_back", utcnow(), who
    release.restored_label, release.closed_reason = target, why
    _touch(release)
    record(db, "release.rolled_back", "release", release.id,
           {"agent": agent_row.name, "from": release.candidate_label, "to": target, "reason": why}, actor=who)
    db.flush()
    return _out(db, release)


def cancel(db: Session, release_id: str, *, actor: str, reason: str | None = None) -> dict[str, Any]:
    who = _name(actor, "actor")
    release = _get(db, release_id)
    check_action(release.state, "cancel")
    release.state, release.closed_reason = "cancelled", reason or f"cancelled by {who}"
    _touch(release)
    record(db, "release.cancelled", "release", release.id,
           {"agent": get_agent_name(db, release.agent_id), "candidate": release.candidate_label, "reason": reason}, actor=who)
    db.flush()
    return _out(db, release)


# --------------------------------------------------------------------------- reads
def get_release(db: Session, release_id: str) -> dict[str, Any]:
    return _out(db, _get(db, release_id))


def list_releases(db: Session, agent_name: str, state: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    agent_row = get_agent(db, agent_name)
    stmt = select(Release).where(Release.agent_id == agent_row.id)
    if state:
        stmt = stmt.where(Release.state == state)
    rows = db.scalars(stmt.order_by(Release.created_at.desc()).limit(max(1, min(limit, 100)))).all()
    return [release_to_dict(db, r, agent_row.name) for r in rows]


def audit_events(
    db: Session, *, entity_id: str | None = None, entity_type: str | None = None, action_prefix: str | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    stmt = select(AuditEvent)
    if entity_id:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if action_prefix:
        stmt = stmt.where(AuditEvent.action.like(action_prefix.replace("%", "").replace("_", r"\_") + "%", escape="\\"))
    rows = db.scalars(stmt.order_by(AuditEvent.created_at.desc(), AuditEvent.id).limit(max(1, min(limit, 500)))).all()
    return [
        {"id": e.id, "created_at": e.created_at, "actor": e.actor, "action": e.action,
         "entity_type": e.entity_type, "entity_id": e.entity_id, "detail": e.detail}
        for e in rows
    ]
