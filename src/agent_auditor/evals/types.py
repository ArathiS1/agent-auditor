"""Plain data types shared by the eval engine. No database or web imports here, so the whole
engine can be tested without a server."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class StepView:
    kind: str
    name: str
    status: str = "ok"
    error: str | None = None
    input: Any = None
    output: Any = None
    attributes: dict[str, Any] = field(default_factory=dict)
    latency_ms: float | None = None


@dataclass(frozen=True)
class RunView:
    id: str
    status: str = "ok"
    error: str | None = None
    input: Any = None
    output: Any = None
    latency_ms: float | None = None
    steps: tuple[StepView, ...] = ()
    tags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreResult:
    """Outcome of one scorer on one run. `score` is 0..1; `passed` is the pass/fail decision."""

    score: float
    passed: bool
    reason: str
    details: dict[str, Any] = field(default_factory=dict)


# A scorer returns None when it does not apply to a run (e.g. a retrieval scorer on a run that
# never retrieved anything). "Not applicable" is recorded, but never counted as pass or fail.
ScorerFn = Callable[[RunView, dict[str, Any]], "ScoreResult | None"]


@dataclass(frozen=True)
class ScorerSpec:
    name: str
    fn: ScorerFn
    description: str
    defaults: dict[str, Any] = field(default_factory=dict)
    default_enabled: bool = True
