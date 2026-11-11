"""Root-cause classification of a single run, from its recorded evidence.

How it decides (all rules are explicit and deterministic; there is no model call)
1. Hard failures dominate. If any step raised, or the run itself errored, the *earliest* erroring
   step is the primary cause: a tool timeout, tool error, LLM error or retrieval error, refined by
   the error text (timeout, rate limit, context length, ...). A run that errored with no step to
   blame is an agent_exception (a bug or unhandled case in the agent's own code).
2. Otherwise quality symptoms are ranked from upstream to downstream: retrieval returned nothing,
   retrieval returned only weak matches, the answer is not supported by what was retrieved,
   the run was too slow (blamed on its slowest step), or the output was empty.
3. Everything else that also fired is listed as contributing, never dropped.

`strength` says how direct the evidence is, NOT a probability: strong = observed directly (an error
message, an empty result), moderate = threshold-based or status-only, weak = a heuristic scorer.
It is measured against synthetic failures in rootcause.benchmark; real accuracy is unknown until you
check diagnoses against your own incidents.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..evals.scorers import _doc_count, evaluate_run
from ..evals.types import RunView, StepView
from .errors import error_kind, is_informative

CLASSIFIER_VERSION = "1"

# cause -> (what it means, suggested first checks). The advice is generic guidance, not a diagnosis.
TAXONOMY: dict[str, dict[str, Any]] = {
    "retrieval_error": {
        "meaning": "The retrieval step itself raised (vector store down, bad index, wrong embedding dimension).",
        "advice": ["Check the vector store/service health and recent index or embedding-model changes.",
                   "Look at the error text of the failing retrieval step."],
    },
    "tool_timeout": {
        "meaning": "A tool call timed out.",
        "advice": ["Check the tool's latency and availability around the failure times.",
                   "Review the timeout setting and whether retries or a fallback exist."],
    },
    "tool_error": {
        "meaning": "A tool call failed for a reason other than a timeout (connection, HTTP error, bad arguments).",
        "advice": ["Read the error text of the failing tool step; the sub-cause says what kind it was.",
                   "Check whether the arguments the agent passed are valid for the tool."],
    },
    "llm_error": {
        "meaning": "The model call failed (rate limit, context too long, auth, provider outage, timeout).",
        "advice": ["The sub-cause says which: rate limit -> add backoff or raise quota; context length -> trim or summarise inputs.",
                   "Check provider status and the model/key configuration."],
    },
    "agent_exception": {
        "meaning": "The run failed without any step failing: an unhandled error in the agent's own code (parsing, state, logic).",
        "advice": ["Reproduce with the failing input (see regression tests) and read the stack trace.",
                   "Parsing the model's output is a common source."],
    },
    "retrieval_empty": {
        "meaning": "Retrieval returned no documents, so the answer had nothing to be grounded in.",
        "advice": ["Check whether the knowledge base covers these topics at all (see the failing inputs).",
                   "Check query rewriting and filters; consider a fallback when nothing is found."],
    },
    "retrieval_low_relevance": {
        "meaning": "Documents were returned but the best match was weak.",
        "advice": ["Compare the questions with the closest documents; the content may be missing or phrased differently.",
                   "Check chunking, embedding model and top_k changes."],
    },
    "ungrounded_answer": {
        "meaning": "Retrieval looked fine but the answer is not supported by the retrieved text (heuristic).",
        "advice": ["Read a few of these answers against their retrieved documents; this signal is a heuristic.",
                   "Check the prompt's grounding instructions and the model version."],
    },
    "slow_run": {
        "meaning": "The run exceeded its latency budget; the slowest step is named.",
        "advice": ["Look at the named step's latency over time; compare with the drift report.",
                   "Check for added steps, larger inputs or a slower model."],
    },
    "empty_output": {
        "meaning": "The run finished without error but produced no output.",
        "advice": ["Check how the final answer is extracted from the model response.",
                   "Look at the last LLM step's output for these runs."],
    },
    "wrong_answer": {
        "meaning": "The run finished without error but its answer did not meet what the test expected, was a non-answer ('I am not sure'), or was graded as wrong by a judge model.",
        "advice": ["Read the failed expectation in the run's evidence, then compare the answer with what was expected.",
                   "If many models miss the same question, the question or the expected answer may be wrong; otherwise check the prompt and the model."],
    },
    "user_rejected": {
        "meaning": "A person marked the answer as bad (thumbs down), although nothing errored.",
        "advice": ["Read the comment the person left, if any, and the answer itself.",
                   "Turn the run into a regression test so the same mistake is caught next time."],
    },
    "unknown": {
        "meaning": "A failure was recorded but no rule could attribute it (for example a custom judge failed).",
        "advice": ["Inspect the run's steps and the failed scorers listed in the evidence."],
    },
}
CAUSES = tuple(TAXONOMY)

_RANK = {"strong": 3, "moderate": 2, "weak": 1}
# Upstream-to-downstream order for quality symptoms when nothing errored.
_SOFT_ORDER = ("retrieval_empty", "retrieval_low_relevance", "ungrounded_answer", "wrong_answer", "user_rejected", "slow_run", "empty_output")
_DEFAULT_SCORERS = ("run_succeeded", "no_step_errors", "output_present", "retrieval_nonempty",
                    "retrieval_top_score", "latency_within_budget", "grounded_overlap")
_KNOWN_SCORERS = frozenset(_DEFAULT_SCORERS)


@dataclass(frozen=True)
class Finding:
    cause: str
    strength: str  # strong | moderate | weak
    evidence: tuple[str, ...]
    step: str | None = None
    subcause: str | None = None
    position: int | None = None  # index of the step in the run, if the finding belongs to one

    def as_dict(self) -> dict[str, Any]:
        return {"cause": self.cause, "strength": self.strength, "evidence": list(self.evidence),
                "step": self.step, "subcause": self.subcause, "position": self.position}


@dataclass(frozen=True)
class Diagnosis:
    status: str  # healthy | failed
    primary: str | None
    strength: str | None
    subcause: str | None
    step: str | None
    findings: tuple[Finding, ...] = ()
    symptoms: tuple[str, ...] = ()  # names of the failed scorers
    explanation: str = ""
    classifier_version: str = CLASSIFIER_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "cause": self.primary, "strength": self.strength, "subcause": self.subcause,
            "step": self.step, "findings": [f.as_dict() for f in self.findings], "symptoms": list(self.symptoms),
            "explanation": self.explanation, "classifier_version": self.classifier_version,
        }


def _short(text: str | None, n: int = 140) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _error_finding(index: int, step: StepView) -> Finding:
    text = step.error or ""
    kind = error_kind(text)
    if step.kind == "retrieval":
        cause = "retrieval_error"
    elif step.kind == "llm":
        cause = "llm_error"
    else:  # tool and "other" steps
        cause = "tool_timeout" if kind == "timeout" else "tool_error"
    informative = bool(text.strip())
    strength = "strong" if informative else "moderate"
    evidence = [f"step '{step.name}' ({step.kind}) failed" + (f": {_short(text)}" if informative else " with no error message")]
    subcause = None if cause == "tool_timeout" else (kind if kind != "other" else None)
    if cause == "tool_timeout":
        subcause = "timeout"
    if not is_informative(text) and informative:
        evidence.append("error text has no recognisable keyword, so the sub-cause is unknown")
    return Finding(cause, strength, tuple(evidence), step.name, subcause, index)


def classify_run(
    run: RunView,
    *,
    params: Mapping[str, Mapping[str, Any]] | None = None,
    extra_failed: Sequence[str] = (),
) -> Diagnosis:
    """Diagnose one run. `params` are scorer parameters (thresholds, budget) so the classifier
    agrees with how failures were judged. `extra_failed` are names of other scorers (for example
    custom judges) already known to have failed on this run."""
    results, _errors = evaluate_run(run, list(_DEFAULT_SCORERS), dict(params or {}))
    failed = {name for name, r in results.items() if r is not None and not r.passed}
    extra = [n for n in extra_failed if n not in _KNOWN_SCORERS]
    failed |= {n for n in extra_failed if n in _KNOWN_SCORERS}

    hard: list[Finding] = []
    soft: list[Finding] = []

    # ---- hard failures
    for i, step in enumerate(run.steps):
        if step.status == "error":
            hard.append(_error_finding(i, step))
    if run.status == "error" and not any(f.position is not None for f in hard):
        kind = error_kind(run.error)
        informative = bool((run.error or "").strip())
        ev = [f"run failed: {_short(run.error)}" if informative else "run failed with no error message",
              "no individual step reported an error, so the failure is in the agent's own code or flow"]
        hard.append(Finding("agent_exception", "moderate", tuple(ev), None, kind if kind != "other" else None, None))

    # ---- soft symptoms (a failed scorer plus the steps that explain it)
    retrieval = [(i, s) for i, s in enumerate(run.steps) if s.kind == "retrieval" and s.status != "error"]
    min_docs = int(((params or {}).get("retrieval_nonempty") or {}).get("min_docs", 1))
    if "retrieval_nonempty" in failed:
        empty = [(i, s) for i, s in retrieval if (_doc_count(s) is not None and _doc_count(s) < min_docs)]
        if empty:
            i, s = empty[0]
            n = _doc_count(s)
            soft.append(Finding("retrieval_empty", "strong", (f"retrieval step '{s.name}' returned {n} document(s)",), s.name, None, i))
    if "retrieval_top_score" in failed and not any(f.cause == "retrieval_empty" for f in soft):
        threshold = float(((params or {}).get("retrieval_top_score") or {}).get("threshold", 0.5))
        worst: tuple[float, int, StepView] | None = None
        for i, s in retrieval:
            scores = s.attributes.get("scores")
            nums = [float(x) for x in scores if isinstance(x, (int, float)) and not isinstance(x, bool)] if isinstance(scores, (list, tuple)) else []
            if nums and (worst is None or max(nums) < worst[0]):
                worst = (max(nums), i, s)
        if worst is not None:
            top, i, s = worst
            soft.append(Finding("retrieval_low_relevance", "moderate",
                                (f"best retrieval score in '{s.name}' is {top:.2f}, below the {threshold:.2f} threshold",), s.name, None, i))
    if "grounded_overlap" in failed:
        soft.append(Finding("ungrounded_answer", "weak", ("answer terms are mostly absent from the retrieved text (heuristic)",)))
    if "latency_within_budget" in failed:
        timed = [(s.latency_ms, i, s) for i, s in enumerate(run.steps) if s.latency_ms is not None]
        ev = [f"run took {run.latency_ms:.0f} ms" if run.latency_ms is not None else "run exceeded its latency budget"]
        step_name = sub = None
        pos = None
        if timed:
            ms, pos, slowest = max(timed, key=lambda t: t[0])
            share = ms / run.latency_ms if run.latency_ms else None
            step_name, sub = slowest.name, slowest.kind
            ev.append(f"slowest step is '{slowest.name}' ({slowest.kind}) at {ms:.0f} ms" + (f", {share:.0%} of the run" if share else ""))
        soft.append(Finding("slow_run", "moderate", tuple(ev), step_name, sub, pos))
    if "output_present" in failed and not hard:
        soft.append(Finding("empty_output", "moderate", ("run finished without error but its output is empty",)))
    for name in extra:
        if name == "expectations_met":
            soft.append(Finding("wrong_answer", "moderate", ("the answer did not meet the expectations tagged on this run",)))
        elif name == "gives_an_answer":
            soft.append(Finding("wrong_answer", "moderate", ("the answer was a non-answer such as 'I am not sure' or 'I can't help with that'",)))
        elif name == "answer_judge":
            soft.append(Finding("wrong_answer", "moderate", ("the judge model graded this answer as not correct",)))
        elif name == "human_feedback":
            soft.append(Finding("user_rejected", "strong", ("a person marked this answer as bad",)))
        else:
            soft.append(Finding("unknown", "weak", (f"scorer '{name}' failed and no rule explains it",)))

    # ---- choose the primary cause
    if hard:
        primary = min(hard, key=lambda f: (f.position if f.position is not None else 10**9))
    elif soft:
        primary = min(soft, key=lambda f: _SOFT_ORDER.index(f.cause) if f.cause in _SOFT_ORDER else len(_SOFT_ORDER))
    elif failed:
        primary = Finding("unknown", "weak", (f"scorer(s) failed without a matching rule: {', '.join(sorted(failed))}",))
        soft.append(primary)
    else:
        return Diagnosis("healthy", None, None, None, None)

    others = [f for f in hard + soft if f is not primary]
    others.sort(key=lambda f: (-_RANK[f.strength], f.cause))
    ordered = (primary, *others)
    explanation = primary.evidence[0]
    if others:
        explanation += "; also: " + "; ".join(f.evidence[0] for f in others[:3])
    return Diagnosis("failed", primary.cause, primary.strength, primary.subcause, primary.step, ordered,
                     tuple(sorted(failed | set(extra))), explanation)
