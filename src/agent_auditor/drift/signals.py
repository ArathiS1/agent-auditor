"""Turn one run into the numbers, flags and term sets that drift detection compares."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..evals.scorers import _doc_count, _extract_text, _retrieval_steps, _tokens
from ..evals.types import RunView

NUMERIC_SIGNALS = ("latency_ms", "input_chars", "output_chars", "step_count", "retrieval_top_score", "retrieval_doc_count")
RATE_SIGNALS = ("run_error", "step_error", "empty_retrieval")
BAG_SIGNALS = ("input_terms", "tool_calls")


@dataclass(frozen=True)
class RunSignals:
    numeric: dict[str, float] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)
    bags: dict[str, frozenset[str]] = field(default_factory=dict)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    pieces: list[str] = []
    _extract_text(value, pieces, [20_000])
    return " ".join(pieces) if pieces else None


def extract_signals(run: RunView) -> RunSignals:
    """A signal that does not apply to a run (no retrieval, no latency...) is simply absent."""
    numeric: dict[str, float] = {"step_count": float(len(run.steps))}
    flags: dict[str, bool] = {"run_error": run.status == "error"}
    bags: dict[str, frozenset[str]] = {}

    if run.latency_ms is not None:
        numeric["latency_ms"] = float(run.latency_ms)

    in_text, out_text = _text(run.input), _text(run.output)
    if in_text is not None:
        numeric["input_chars"] = float(len(in_text))
        terms = _tokens(in_text)
        if terms:
            bags["input_terms"] = frozenset(terms)
    if out_text is not None:
        numeric["output_chars"] = float(len(out_text))

    if run.steps:
        flags["step_error"] = any(s.status == "error" for s in run.steps)
        tools = frozenset(s.name for s in run.steps if s.kind == "tool")
        if tools:
            bags["tool_calls"] = tools

    retrieval = _retrieval_steps(run)
    if retrieval:
        top_scores: list[float] = []
        counts: list[int] = []
        for step in retrieval:
            scores = step.attributes.get("scores")
            if isinstance(scores, (list, tuple)):
                top_scores.extend(float(x) for x in scores if isinstance(x, (int, float)) and not isinstance(x, bool))
            n = _doc_count(step)
            if n is not None:
                counts.append(n)
        if top_scores:
            numeric["retrieval_top_score"] = max(top_scores)
        if counts:
            numeric["retrieval_doc_count"] = float(sum(counts))
            flags["empty_retrieval"] = any(n == 0 for n in counts)
    return RunSignals(numeric, flags, bags)
