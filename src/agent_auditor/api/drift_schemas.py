from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DriftCheckIn(BaseModel):
    agent: str
    environment: Literal["production", "canary", "test"] = "production"
    version: str | None = Field(default=None, description="Only look at runs of this version label")
    current_hours: float = Field(default=24.0, gt=0, le=24 * 365, description="Length of the recent window")
    reference_hours: float = Field(default=168.0, gt=0, le=24 * 365 * 5, description="Length of the baseline window that ends where the recent one starts")
    as_of: datetime | None = Field(default=None, description="End of the recent window; defaults to now")
    min_samples: int = Field(default=30, ge=5, le=100_000, description="Minimum observations per window for a signal to be tested")
    alpha: float = Field(default=0.01, gt=0, lt=1, description="Family-wise false alarm rate, split across signals")
    correct_multiple: bool = True
    include_evals: bool = Field(default=True, description="Also compare eval pass rates (needs runs scored by /v1/evals/run)")
    max_runs: int = Field(default=2000, ge=50, le=5000, description="Newest runs analysed per window")
    save: bool = True


class DriftSignalOut(BaseModel):
    name: str
    kind: str
    severity: str
    significant: bool
    p_value: float | None
    alpha_used: float
    effect_name: str
    effect: float | None
    reference_n: int
    current_n: int
    reference: dict[str, Any]
    current: dict[str, Any]
    reason: str
    details: dict[str, Any]


class DriftReportOut(BaseModel):
    id: str | None = None
    created_at: datetime | None = None
    agent: str
    environment: str
    version: str | None
    as_of: datetime
    windows: dict[str, datetime]
    overall: str
    reference_runs: int
    current_runs: int
    reference_total_runs: int
    current_total_runs: int
    alpha: float
    min_samples: int
    notes: list[str]
    signals: list[DriftSignalOut]


class DriftReportSummary(BaseModel):
    id: str
    created_at: datetime
    as_of: datetime
    environment: str
    version: str | None
    overall: str
    reference_runs: int
    current_runs: int
    flagged: list[str]
