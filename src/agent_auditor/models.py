"""Database models.

Design notes
- Everything an agent does is a Run made of ordered Steps (llm / retrieval / tool / other).
  Later phases (evals, drift, root cause) all read from these two tables.
- AgentVersion stores the prompt + config that produced a run, identified by a content hash.
  This is what makes regression comparison, canaries and rollback possible later.
- AuditEvent is an append-only record of governance-relevant changes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    versions: Mapped[list["AgentVersion"]] = relationship(back_populates="agent")
    runs: Mapped[list["Run"]] = relationship(back_populates="agent")


class AgentVersion(Base):
    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "label"), UniqueConstraint("agent_id", "content_hash"))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    label: Mapped[str] = mapped_column(String(100))
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    agent: Mapped[Agent] = relationship(back_populates="versions")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    version_id: Mapped[str | None] = mapped_column(ForeignKey("agent_versions.id"), nullable=True, index=True)
    environment: Mapped[str] = mapped_column(String(20), default="production", index=True)
    status: Mapped[str] = mapped_column(String(10), default="ok", index=True)
    input_data: Mapped[Any] = mapped_column("input", JSON, nullable=True)
    output_data: Mapped[Any] = mapped_column("output", JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    agent: Mapped[Agent] = relationship(back_populates="runs")
    version: Mapped[AgentVersion | None] = relationship()
    steps: Mapped[list["Step"]] = relationship(
        back_populates="run", order_by="Step.position", cascade="all, delete-orphan"
    )


class Step(Base):
    __tablename__ = "steps"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(200))
    input_data: Mapped[Any] = mapped_column("input", JSON, nullable=True)
    output_data: Mapped[Any] = mapped_column("output", JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="ok")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    run: Mapped[Run] = relationship(back_populates="steps")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class EvalResult(Base):
    """One scorer's verdict on one run. A row exists for every (run, scorer) pair that was
    evaluated, including `applicable=False` rows (the scorer did not apply to that run), so that
    a run counts as evaluated and is not picked up again."""

    __tablename__ = "eval_results"
    __table_args__ = (UniqueConstraint("run_id", "scorer"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    scorer: Mapped[str] = mapped_column(String(100), index=True)
    applicable: Mapped[bool] = mapped_column(Boolean, default=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DriftReport(Base):
    """A saved drift check: the full result (every signal, with reasons) as JSON, so reports can be
    listed, compared over time and shown on a dashboard without recomputing."""

    __tablename__ = "drift_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    environment: Mapped[str] = mapped_column(String(20))
    version_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    overall: Mapped[str] = mapped_column(String(20), index=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RegressionCase(Base):
    """A regression test distilled from a real failure: the input that failed plus what a future
    run must satisfy. `fingerprint` is unique per agent, so the same failure is stored once and
    later sightings only raise `occurrences`."""

    __tablename__ = "regression_cases"
    __table_args__ = (UniqueConstraint("agent_id", "fingerprint"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(20), default="auto")  # auto | manual
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    origin_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    environment: Mapped[str] = mapped_column(String(20), default="production")
    input_data: Mapped[Any] = mapped_column("input", JSON, nullable=True)
    reference_output: Mapped[Any] = mapped_column(JSON, nullable=True)  # what the failing run answered
    expectations: Mapped[list[Any]] = mapped_column(JSON, default=list)
    failure: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # scorers, reasons, error (read by root-cause phase)
    merged_run_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)  # later runs folded into this case
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    agent: Mapped[Agent] = relationship()


class ReplayRun(Base):
    """One replay of the suite against a candidate version: a verdict plus per-case results."""

    __tablename__ = "replay_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    version_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(String(20), index=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    results: Mapped[list["ReplayResult"]] = relationship(back_populates="replay", cascade="all, delete-orphan")


class ReplayResult(Base):
    __tablename__ = "replay_results"
    __table_args__ = (UniqueConstraint("replay_id", "case_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    replay_id: Mapped[str] = mapped_column(ForeignKey("replay_runs.id"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("regression_cases.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    outcome: Mapped[str] = mapped_column(String(12), index=True)  # pass | fail | unverified | missing
    previous_outcome: Mapped[str | None] = mapped_column(String(12), nullable=True)
    checks: Mapped[list[Any]] = mapped_column(JSON, default=list)

    replay: Mapped[ReplayRun] = relationship(back_populates="results")


class RunDiagnosis(Base):
    """The root-cause classifier's verdict on one run, stored so reports and later phases (canary
    comparison, approval gates) can aggregate causes without re-reading every step."""

    __tablename__ = "run_diagnoses"
    __table_args__ = (UniqueConstraint("run_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    version_label: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    environment: Mapped[str] = mapped_column(String(20), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)  # copied from the run
    status: Mapped[str] = mapped_column(String(10), index=True)  # healthy | failed
    cause: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    strength: Mapped[str | None] = mapped_column(String(10), nullable=True)
    subcause: Mapped[str | None] = mapped_column(String(40), nullable=True)
    step: Mapped[str | None] = mapped_column(String(200), nullable=True)
    findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    symptoms: Mapped[list[Any]] = mapped_column(JSON, default=list)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    classifier_version: Mapped[str] = mapped_column(String(10))
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CanaryReport(Base):
    """One canary evaluation: the verdict and every check behind it, stored as JSON. Approval gates
    (Phase 8) read the latest report for a candidate version."""

    __tablename__ = "canary_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    baseline_label: Mapped[str] = mapped_column(String(100))
    candidate_label: Mapped[str] = mapped_column(String(100), index=True)
    verdict: Mapped[str] = mapped_column(String(20), index=True)  # promote | hold | rollback | continue
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Release(Base):
    """A request to make a candidate version the active one, with its gate policy and outcome."""

    __tablename__ = "releases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    candidate_label: Mapped[str] = mapped_column(String(100), index=True)
    baseline_label: Mapped[str | None] = mapped_column(String(100), nullable=True)  # active when proposed
    state: Mapped[str] = mapped_column(String(20), default="proposed", index=True)
    proposed_by: Mapped[str] = mapped_column(String(100))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    previous_active_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    restored_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    closed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)  # why it was rejected, cancelled or rolled back

    approvals: Mapped[list["ReleaseApproval"]] = relationship(
        back_populates="release", cascade="all, delete-orphan", order_by="ReleaseApproval.created_at"
    )


class ReleaseApproval(Base):
    """One person's decision on a release, tied to the canary evidence they saw."""

    __tablename__ = "release_approvals"
    __table_args__ = (UniqueConstraint("release_id", "approver"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    release_id: Mapped[str] = mapped_column(ForeignKey("releases.id"), index=True)
    approver: Mapped[str] = mapped_column(String(100))
    decision: Mapped[str] = mapped_column(String(10))  # approve | reject
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    canary_report_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # that report's time

    release: Mapped[Release] = relationship(back_populates="approvals")
