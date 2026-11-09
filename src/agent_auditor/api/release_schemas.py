from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class GatePolicyIn(BaseModel):
    required_approvals: int = Field(default=1, ge=1, le=10)
    allow_self_approval: bool = False
    max_evidence_age_hours: float = Field(default=24.0, gt=0, le=24 * 30)
    require_replay: bool = False
    require_baseline_is_active: bool = True


class ProposeIn(BaseModel):
    candidate: str = Field(min_length=1, max_length=100)
    proposed_by: str = Field(min_length=1, max_length=100, description="Who is asking (self-asserted label, not authenticated)")
    notes: str | None = Field(default=None, max_length=2000)
    policy: GatePolicyIn = Field(default_factory=GatePolicyIn)


class DecideIn(BaseModel):
    approver: str = Field(min_length=1, max_length=100)
    decision: Literal["approve", "reject"] = "approve"
    comment: str | None = Field(default=None, max_length=2000)


class PromoteIn(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class RollbackIn(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    to_version: str | None = Field(default=None, max_length=100, description="Default: the version that was active before")


class CancelIn(BaseModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str | None = Field(default=None, max_length=2000)


class ApprovalOut(BaseModel):
    approver: str
    decision: str
    comment: str | None
    created_at: datetime
    canary_report_id: str | None


class GateCheckOut(BaseModel):
    name: str
    passed: bool
    reason: str


class GateOut(BaseModel):
    ready: bool
    evidence_ok: bool
    checks: list[GateCheckOut]
    valid_approvers: list[str]
    stale_approvers: list[str]
    blockers: list[str]


class ReleaseOut(BaseModel):
    id: str
    agent: str
    candidate: str
    baseline: str | None
    state: str
    proposed_by: str
    notes: str | None
    policy: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    promoted_at: datetime | None
    promoted_by: str | None
    previous_active: str | None
    rolled_back_at: datetime | None
    rolled_back_by: str | None
    restored: str | None
    closed_reason: str | None
    approvals: list[ApprovalOut]
    gate: GateOut | None
    recommendation: str


class AuditEventOut(BaseModel):
    id: str
    created_at: datetime
    actor: str
    action: str
    entity_type: str
    entity_id: str
    detail: dict[str, Any]
