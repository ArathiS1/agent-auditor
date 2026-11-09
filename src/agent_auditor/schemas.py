from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

StepKind = Literal["llm", "retrieval", "tool", "other"]
Status = Literal["ok", "error"]
Environment = Literal["production", "canary", "test"]


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class _Timed(BaseModel):
    started_at: datetime | None = None
    ended_at: datetime | None = None

    @field_validator("started_at", "ended_at")
    @classmethod
    def _normalise(cls, v: datetime | None) -> datetime | None:
        return _utc(v)

    @model_validator(mode="after")
    def _order(self):
        if self.started_at and self.ended_at and self.ended_at < self.started_at:
            raise ValueError("ended_at must not be before started_at")
        return self


# ---------- ingestion ----------
class StepIn(_Timed):
    kind: StepKind = "other"
    name: str = Field(min_length=1, max_length=200)
    input: Any = None
    output: Any = None
    status: Status = "ok"
    error: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class RunIn(_Timed):
    agent: str = Field(min_length=1, max_length=200)
    version: str | None = Field(default=None, description="Registered version label; defaults to the active version")
    environment: Environment = "production"
    input: Any = None
    output: Any = None
    status: Status = "ok"
    error: str | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    steps: list[StepIn] = Field(default_factory=list, max_length=1000)


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class VersionIn(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    prompt: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    activate: bool = True


# ---------- responses ----------
class AgentOut(BaseModel):
    id: str
    name: str
    description: str | None
    created_at: datetime


class VersionOut(BaseModel):
    id: str
    agent: str
    label: str
    prompt: str | None
    config: dict[str, Any]
    content_hash: str
    is_active: bool
    created_at: datetime


class StepOut(BaseModel):
    id: str
    position: int
    kind: str
    name: str
    input: Any
    output: Any
    status: str
    error: str | None
    started_at: datetime | None
    ended_at: datetime | None
    latency_ms: float | None
    attributes: dict[str, Any]


class RunSummary(BaseModel):
    id: str
    agent: str
    version: str | None
    environment: str
    status: str
    input: Any
    output: Any
    error: str | None
    started_at: datetime
    ended_at: datetime | None
    latency_ms: float | None
    tags: dict[str, Any]
    step_count: int


class RunDetail(RunSummary):
    steps: list[StepOut]
