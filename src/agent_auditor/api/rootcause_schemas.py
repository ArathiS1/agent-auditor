from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Environment = Literal["production", "canary", "test"]


class DiagnoseIn(BaseModel):
    environment: Environment = "production"
    version: str | None = None
    since_hours: float | None = Field(default=None, gt=0, le=24 * 365 * 5)
    limit: int = Field(default=200, ge=1, le=500, description="Runs to classify in this call (newest first)")
    reclassify: bool = Field(default=False, description="Re-diagnose runs that already have a diagnosis")


class DiagnoseOut(BaseModel):
    agent: str
    classified: int
    failed: int
    healthy: int
    has_more: bool
    by_cause: dict[str, int]


class DiagnosisOut(BaseModel):
    run_id: str
    version: str | None
    environment: str
    status: str
    cause: str | None
    strength: str | None
    subcause: str | None
    step: str | None
    findings: list[dict[str, Any]]
    symptoms: list[str]
    explanation: str | None
    classifier_version: str
    classified_at: datetime


class RootCauseIn(BaseModel):
    environment: Environment = "production"
    version: str | None = Field(default=None, description="Restrict to one version (disables the version comparison)")
    since_hours: float | None = Field(default=None, gt=0, le=24 * 365 * 5)
    diagnose_first: bool = Field(default=True, description="Classify undiagnosed runs before reporting")
    alpha: float = Field(default=0.01, gt=0, lt=1)
    min_runs: int = Field(default=20, ge=5, le=100_000, description="Runs a version needs to be compared")
    min_count: int = Field(default=5, ge=1, le=100_000, description="Failures of a cause needed to test it")
    min_effect: float = Field(default=0.10, gt=0, le=1.0, description="Smallest rate difference worth reporting")


class RootCauseOut(BaseModel):
    agent: str
    environment: str
    since_hours: float | None
    version: str | None
    classifier_version: str
    diagnosed_now: int
    total_runs: int
    failed_runs: int
    healthy_runs: int
    failure_rate: float | None
    causes: list[dict[str, Any]]
    version_associations: dict[str, Any]
    suspect_sources: list[dict[str, Any]] = []
    notes: list[str]


class TaxonomyEntry(BaseModel):
    cause: str
    meaning: str
    advice: list[str]
