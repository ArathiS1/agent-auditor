from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Environment = Literal["production", "canary", "test"]


class CanaryPolicyIn(BaseModel):
    min_canary_runs: int = Field(default=30, ge=1, le=1_000_000)
    min_baseline_runs: int = Field(default=30, ge=1, le=1_000_000)
    min_eval_samples: int = Field(default=20, ge=1, le=1_000_000)
    min_effect: float = Field(default=0.05, ge=0, le=1)
    rollback_effect: float = Field(default=0.10, ge=0, le=1)
    alpha: float = Field(default=0.01, gt=0, lt=1)
    latency_warn_ratio: float = Field(default=1.3, ge=1)
    latency_fail_ratio: float = Field(default=2.0, ge=1)
    latency_rollback_ratio: float = Field(default=4.0, ge=1)
    require_replay: bool = False
    replay_rollback_min: int = Field(default=2, ge=1)
    replay_rollback_share: float = Field(default=0.10, ge=0, le=1)
    warnings_block: bool = False
    include_drift: bool = True


class CanaryEvaluateIn(BaseModel):
    baseline: str = Field(description="Version label running in production")
    candidate: str = Field(description="Version label being canaried")
    baseline_environment: Environment = "production"
    candidate_environment: Environment = "canary"
    since_hours: float = Field(default=24.0, gt=0, le=24 * 365)
    policy: CanaryPolicyIn = Field(default_factory=CanaryPolicyIn)
    evaluate_first: bool = Field(default=True, description="Score unscored runs on both sides first")
    scorers: list[str] | None = Field(
        default=None, max_length=30,
        description="Scorers to run first. Omit for the defaults; add 'answer_judge' to have a model grade each answer.")
    diagnose_first: bool = Field(default=True, description="Diagnose undiagnosed runs on both sides first")
    save: bool = True


class CheckOut(BaseModel):
    name: str
    status: Literal["pass", "warn", "fail", "skipped"]
    severity: Literal["none", "minor", "major"]
    reason: str
    details: dict[str, Any]


class SideOut(BaseModel):
    version: str
    environment: str
    runs: int


class CanaryReportOut(BaseModel):
    id: str | None = None
    created_at: datetime | None = None
    agent: str
    baseline: SideOut
    candidate: SideOut
    window: dict[str, Any]
    verdict: Literal["promote", "hold", "rollback", "continue"]
    reasons: list[str]
    blocking: list[str]
    warnings: list[str]
    checks: list[CheckOut]
    policy: dict[str, Any]
    caveat: str


class CanarySummaryOut(BaseModel):
    id: str
    created_at: datetime
    agent: str
    baseline: str
    candidate: str
    verdict: str
    blocking: list[str]
    reasons: list[str]
