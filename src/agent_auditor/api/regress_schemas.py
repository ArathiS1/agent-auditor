from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class GenerateIn(BaseModel):
    environment: Literal["production", "canary", "test"] = "production"
    since_hours: float | None = Field(default=None, gt=0, le=24 * 365 * 5, description="Only failures from the last N hours")
    scorers: list[str] | None = Field(default=None, description="Only treat these scorers' failures as test material")
    limit: int = Field(default=50, ge=1, le=500, description="Maximum new test cases to create in one call")
    include_weak: bool = Field(default=False, description="Also build tests from heuristic scorers such as grounded_overlap")
    similarity: float = Field(default=0.8, ge=0.5, le=1.0, description="Token overlap above which two failures count as the same test")
    evaluate_first: bool = Field(default=True, description="Score unevaluated runs before looking for failures")
    redact: bool = Field(default=False, description="Mask emails, phone numbers, card numbers and key-like strings in stored inputs and outputs")
    dry_run: bool = Field(default=False, description="Show what would be created without saving it")


class CaseOut(BaseModel):
    id: str | None
    agent: str
    name: str
    enabled: bool
    created_by: str
    created_at: datetime | None
    last_seen_at: datetime | None
    fingerprint: str
    source_run_id: str | None
    origin_version: str | None
    environment: str
    input: Any
    reference_output: Any
    expectations: list[dict[str, Any]]
    failure: dict[str, Any]
    occurrences: int
    notes: str | None


class GenerateOut(BaseModel):
    agent: str
    dry_run: bool
    scanned_failed_runs: int
    evaluated_runs: int
    created: list[CaseOut]
    merged_duplicates: int
    skipped: dict[str, int]
    notes: list[str]


class CaseFromRunIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    expectations: list[dict[str, Any]] | None = Field(default=None, description="Override; defaults to the run's failed evals")
    redact: bool = False
    notes: str | None = None


class CaseFromRunOut(BaseModel):
    created: bool
    case: CaseOut


class CasePatch(BaseModel):
    enabled: bool | None = None
    name: str | None = Field(default=None, max_length=200)
    notes: str | None = None
    expectations: list[dict[str, Any]] | None = None


class ReplayResultIn(BaseModel):
    case_id: str
    run_id: str


class ReplayIn(BaseModel):
    version: str | None = Field(default=None, description="Version label the replay runs were recorded under")
    token: str | None = Field(default=None, description="Replay token the SDK put on its runs")
    results: list[ReplayResultIn] | None = Field(default=None, description="Explicit case -> run mapping (alternative to token)")
    case_ids: list[str] | None = Field(default=None, description="Cases this replay covers; defaults to all enabled cases")
    label: str | None = Field(default=None, max_length=100)
    notes: str | None = None


class ReplayCaseResult(BaseModel):
    case_id: str
    run_id: str | None
    outcome: str
    previous_outcome: str | None
    checks: list[dict[str, Any]]


class ReplayOut(BaseModel):
    id: str
    agent: str
    version: str | None
    label: str | None
    notes: str | None
    created_at: datetime
    verdict: str
    summary: dict[str, Any]
    results: list[ReplayCaseResult]
