from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class EvalRunIn(BaseModel):
    agent: str | None = Field(default=None, description="Only evaluate runs of this agent")
    version: str | None = Field(default=None, description="Only this version label (requires agent)")
    environment: Literal["production", "canary", "test"] | None = None
    status: Literal["ok", "error"] | None = None
    limit: int = Field(default=200, ge=1, le=500)
    only_unevaluated: bool = Field(default=True, description="Skip runs that already have results for the chosen scorers")
    scorers: list[str] | None = Field(default=None, min_length=1, description="Defaults to all default-enabled scorers")
    params: dict[str, dict[str, Any]] = Field(default_factory=dict, description="Per-scorer parameters, e.g. {'latency_within_budget': {'budget_ms': 3000}}")


class EvalBatchOut(BaseModel):
    evaluated_runs: int
    results_written: int
    scored: int
    not_applicable: int
    scorer_errors: dict[str, str]
    scorer_error_count: int
    has_more: bool


class ScorerInfo(BaseModel):
    name: str
    description: str
    defaults: dict[str, Any]
    default_enabled: bool


class EvalResultOut(BaseModel):
    scorer: str
    applicable: bool
    score: float | None
    passed: bool | None
    reason: str | None
    details: dict[str, Any]
    evaluated_at: datetime


class ScorerStat(BaseModel):
    scorer: str
    n: int
    passed: int
    pass_rate: float | None
    ci_low: float | None
    ci_high: float | None
    mean_score: float | None


class VersionSummary(BaseModel):
    version: str
    runs: int
    evaluated_runs: int
    scorers: list[ScorerStat]


class SummaryOut(BaseModel):
    agent: str
    versions: list[VersionSummary]


class ScorerComparisonOut(BaseModel):
    scorer: str
    verdict: str
    reason: str
    baseline_n: int
    candidate_n: int
    baseline_pass_rate: float | None
    candidate_pass_rate: float | None
    delta: float | None
    p_value: float | None
    alpha_used: float
    baseline_ci: tuple[float, float] | None
    candidate_ci: tuple[float, float] | None
    baseline_mean_score: float | None
    candidate_mean_score: float | None


class CompareOut(BaseModel):
    agent: str
    baseline: str
    candidate: str
    overall_verdict: str
    alpha: float
    min_samples: int
    min_effect: float
    notes: list[str]
    scorers: list[ScorerComparisonOut]
