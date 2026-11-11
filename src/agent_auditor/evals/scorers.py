"""Built-in scorers and the registry.

All built-ins are deterministic and need no API key. They detect *symptoms* (empty retrieval,
low retrieval scores, tool errors, slow runs, answers not supported by retrieved text). They do
not judge answer quality in general; for that, register an LLM judge with `make_judge_scorer`.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .expect import TEXT_KINDS, check_text_expectation
from .types import RunView, ScoreResult, ScorerSpec

_STOPWORDS = frozenset(
    "the and for are but not you your with that this from have has had was were will would can "
    "could should may might its our their them they then than there these those what when where "
    "which who whom why how into onto out off over under about after before also just only very "
    "been being does did done doing any all some such use used using see here more most other".split()
)
_WORD = re.compile(r"[a-z0-9]{3,}")


# ---------------------------------------------------------------- helpers
def _num(params: dict[str, Any], key: str, default: float) -> float:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"parameter '{key}' must be a number, got {value!r}")
    return float(value)


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _extract_text(value: Any, out: list[str], budget: list[int], depth: int = 0) -> None:
    """Collect string leaves from nested JSON-like data, bounded in size and depth."""
    if budget[0] <= 0 or depth > 6:
        return
    if isinstance(value, str):
        out.append(value[: budget[0]])
        budget[0] -= len(value)
    elif isinstance(value, dict):
        for v in value.values():
            _extract_text(v, out, budget, depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _extract_text(v, out, budget, depth + 1)


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS}


def _answer_text(output: Any) -> str | None:
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in ("answer", "output", "text", "response", "content"):
            if isinstance(output.get(key), str):
                return output[key]
    return None


def _retrieval_steps(run: RunView):
    return [s for s in run.steps if s.kind == "retrieval"]


def _doc_count(step) -> int | None:
    n = step.attributes.get("num_docs")
    if isinstance(n, int) and not isinstance(n, bool):
        return n
    if isinstance(step.output, (list, tuple)):
        # The SDK appends a "...(+N more)" marker when it truncates long lists.
        count = len(step.output)
        if count and isinstance(step.output[-1], str) and step.output[-1].startswith("...(+"):
            try:
                return count - 1 + int(step.output[-1][5:].split(" ")[0])
            except ValueError:
                return count - 1
        return count
    return None


# ---------------------------------------------------------------- scorers
def run_succeeded(run: RunView, params: dict[str, Any]) -> ScoreResult:
    ok = run.status == "ok"
    return ScoreResult(1.0 if ok else 0.0, ok, "run completed" if ok else f"run failed: {run.error or 'unknown error'}")


def no_step_errors(run: RunView, params: dict[str, Any]) -> ScoreResult | None:
    if not run.steps:
        return None
    failed = [s for s in run.steps if s.status == "error"]
    score = 1.0 - len(failed) / len(run.steps)
    if not failed:
        return ScoreResult(1.0, True, "no step errors", {"steps": len(run.steps)})
    names = ", ".join(sorted({s.name for s in failed}))
    return ScoreResult(score, False, f"{len(failed)} of {len(run.steps)} steps failed: {names}",
                       {"failed_steps": [s.name for s in failed]})


def output_present(run: RunView, params: dict[str, Any]) -> ScoreResult:
    empty = _is_empty(run.output)
    return ScoreResult(0.0 if empty else 1.0, not empty, "output is empty" if empty else "output present")


def retrieval_nonempty(run: RunView, params: dict[str, Any]) -> ScoreResult | None:
    min_docs = int(_num(params, "min_docs", 1))
    known = [(s, _doc_count(s)) for s in _retrieval_steps(run)]
    known = [(s, n) for s, n in known if n is not None]
    if not known:
        return None
    bad = [s.name for s, n in known if n < min_docs]
    score = 1.0 - len(bad) / len(known)
    if bad:
        return ScoreResult(score, False, f"retrieval returned too few documents in: {', '.join(bad)}",
                           {"min_docs": min_docs, "counts": {s.name: n for s, n in known}})
    return ScoreResult(1.0, True, "retrieval returned documents", {"counts": {s.name: n for s, n in known}})


def retrieval_top_score(run: RunView, params: dict[str, Any]) -> ScoreResult | None:
    threshold = _num(params, "threshold", 0.5)
    tops: dict[str, float] = {}
    for s in _retrieval_steps(run):
        scores = s.attributes.get("scores")
        if isinstance(scores, (list, tuple)):
            nums = [float(x) for x in scores if isinstance(x, (int, float)) and not isinstance(x, bool)]
            if nums:
                tops[s.name] = max(nums)
    if not tops:
        return None
    worst = min(tops.values())
    score = max(0.0, min(1.0, sum(tops.values()) / len(tops)))
    low = [n for n, t in tops.items() if t < threshold]
    if low:
        return ScoreResult(score, False, f"best retrieval score {worst:.2f} is below {threshold:.2f}",
                           {"threshold": threshold, "top_scores": tops})
    return ScoreResult(score, True, f"best retrieval score {worst:.2f} meets {threshold:.2f}",
                       {"threshold": threshold, "top_scores": tops})


def latency_within_budget(run: RunView, params: dict[str, Any]) -> ScoreResult | None:
    if run.latency_ms is None:
        return None
    budget = _num(params, "budget_ms", 10_000)
    if budget <= 0:
        raise ValueError("parameter 'budget_ms' must be positive")
    ok = run.latency_ms <= budget
    score = 1.0 if ok else max(0.0, budget / run.latency_ms)
    return ScoreResult(score, ok, f"{run.latency_ms:.0f} ms vs {budget:.0f} ms budget",
                       {"latency_ms": run.latency_ms, "budget_ms": budget})


def grounded_overlap(run: RunView, params: dict[str, Any]) -> ScoreResult | None:
    """Heuristic: what fraction of the answer's content words appear in the retrieved text?

    This is a cheap proxy for "the answer is supported by what was retrieved". It misses
    paraphrase and can be fooled by repeating context, so treat it as a weak signal.
    """
    threshold = _num(params, "threshold", 0.5)
    answer = _answer_text(run.output)
    if not answer:
        return None
    answer_tokens = _tokens(answer)
    if len(answer_tokens) < 3:
        return None
    pieces: list[str] = []
    budget = [20_000]
    for s in _retrieval_steps(run):
        if s.attributes.get("grounding") is False:  # e.g. web search snippets: the answer is written from whole pages, so overlap says little
            continue
        _extract_text(s.output, pieces, budget)
    if not pieces:
        return None
    context_tokens = _tokens(" ".join(pieces))
    if not context_tokens:
        return None
    overlap = len(answer_tokens & context_tokens) / len(answer_tokens)
    ok = overlap >= threshold
    return ScoreResult(overlap, ok, f"{overlap:.0%} of answer terms found in retrieved text (need {threshold:.0%})",
                       {"threshold": threshold, "answer_terms": len(answer_tokens)})


_NON_ANSWER = re.compile(
    r"\bi(?:'m| am) not (?:really |entirely |completely )?sure\b"
    r"|\bi (?:do not|don't|dont) (?:really )?know\b"
    r"|\bno idea\b"
    r"|\bi (?:cannot|can ?not|can't|am unable to|'m unable to|am not able to|'m not able to|won't|will not) "
    r"(?:help|answer|assist|provide|say|tell|respond|do that|find)"
    r"|\bunable to (?:help|answer|assist|provide)\b"
    r"|\bi (?:do not|don't) have (?:enough |any )?(?:information|access|an answer)\b"
    r"|\bas an ai\b|\bas a language model\b|\bi(?:'m| am) sorry,? but\b",
    re.IGNORECASE,
)


def gives_an_answer(run: RunView, params: dict[str, Any]) -> ScoreResult | None:
    """Heuristic: is the answer a non-answer ("I am not sure", "I don't know", "I can't help with that")?

    It exists for runs that carry no expected answer, which is most real traffic. It only looks at short answers,
    because a long answer that contains one hedge is usually still an answer. A refusal is sometimes the right
    behaviour, so the signal is only useful as a *change*: the canary compares the rate between two versions.
    Runs tagged with expectations are left to `expectations_met`, which knows what the right answer is.
    """
    expect = (run.tags or {}).get("expect")
    if isinstance(expect, list) and expect:
        return None
    if run.status != "ok":
        return None
    answer = _answer_text(run.output)
    if answer is None or not answer.strip():
        return None
    max_words = int(_num(params, "max_words", 30))
    words = len(answer.split())
    if words > max_words:
        return ScoreResult(1.0, True, f"answer has {words} words, longer than a non-answer", {"words": words})
    found = _NON_ANSWER.search(answer.replace("’", "'"))
    if found is None:
        return ScoreResult(1.0, True, "answer is not a refusal or a hedge", {"words": words})
    return ScoreResult(0.0, False, f"answer is a non-answer ('{found.group(0).strip()}')", {"words": words, "matched": found.group(0)})


# ---------------------------------------------------------------- registry
_REGISTRY: dict[str, ScorerSpec] = {}


def register_scorer(spec: ScorerSpec, *, replace: bool = False) -> None:
    if spec.name in _REGISTRY and not replace:
        raise ValueError(f"scorer '{spec.name}' is already registered")
    _REGISTRY[spec.name] = spec


def get_scorers() -> dict[str, ScorerSpec]:
    return dict(_REGISTRY)


def expectations_met(run: RunView, params: dict[str, Any]) -> ScoreResult | None:
    """The answer meets what the run was tagged to expect (tags.expect, a list of text expectations).
    Not applicable to runs that carry no expectations."""
    expect = (run.tags or {}).get("expect")
    if not isinstance(expect, list) or not expect:
        return None
    pieces: list[str] = []
    _extract_text(run.output, pieces, [50_000])
    text = " ".join(pieces)
    results = []
    for e in expect[:20]:
        if not isinstance(e, dict) or e.get("type") not in TEXT_KINDS:
            continue
        ok, reason = check_text_expectation(e, text)
        results.append((ok, reason))
    if not results:
        return None
    missed = [reason for ok, reason in results if not ok]
    score = sum(1 for ok, _ in results if ok) / len(results)
    return ScoreResult(score, not missed, "; ".join(missed) if missed else "answer meets every expectation",
                       {"checks": len(results), "missed": len(missed)})


def default_scorer_names() -> list[str]:
    return [n for n, s in _REGISTRY.items() if s.default_enabled]


def make_judge_scorer(
    name: str,
    judge: Callable[[RunView], "tuple[float, str] | float"],
    *,
    description: str = "Custom judge",
    threshold: float = 0.5,
    default_enabled: bool = False,
) -> ScorerSpec:
    """Wrap your own judge (for example an LLM call) as a scorer.

    `judge(run_view)` returns a score in 0..1, or (score, reason). Judges are never enabled by
    default, because they cost money and can be wrong; list them in `scorers` to use them.
    """

    def fn(run: RunView, params: dict[str, Any]) -> ScoreResult | None:
        out = judge(run)
        score, reason = (out, "judge score") if isinstance(out, (int, float)) else out
        score = float(score)
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"judge '{name}' returned {score}, expected a value between 0 and 1")
        limit = _num(params, "threshold", threshold)
        return ScoreResult(score, score >= limit, str(reason)[:500], {"threshold": limit, "judge": True})

    return ScorerSpec(name, fn, description, {"threshold": threshold}, default_enabled)


for _spec in (
    ScorerSpec("run_succeeded", run_succeeded, "Run finished without an error status"),
    ScorerSpec("no_step_errors", no_step_errors, "No step (tool, LLM or retrieval) reported an error"),
    ScorerSpec("output_present", output_present, "Run produced a non-empty output"),
    ScorerSpec("retrieval_nonempty", retrieval_nonempty, "Every retrieval step returned documents", {"min_docs": 1}),
    ScorerSpec("retrieval_top_score", retrieval_top_score, "Best retrieval similarity score is high enough", {"threshold": 0.5}),
    ScorerSpec("latency_within_budget", latency_within_budget, "Run finished within the latency budget", {"budget_ms": 10_000}),
    ScorerSpec("grounded_overlap", grounded_overlap, "Answer terms appear in retrieved text (heuristic)", {"threshold": 0.5}),
    ScorerSpec("expectations_met", expectations_met, "Answer meets the expectations the run was tagged with (tags.expect)"),
    ScorerSpec("gives_an_answer", gives_an_answer,
               "Answer is not a non-answer such as 'I am not sure' (heuristic, for runs with no expectations)", {"max_words": 30}),
):
    register_scorer(_spec)


def _answer_judge(run: RunView, params: dict[str, Any]) -> ScoreResult | None:
    from .judge import answer_judge

    return answer_judge(run, params)


register_scorer(ScorerSpec(
    "answer_judge", _answer_judge,
    "A model grades each answer from 0 to 1 (off by default; needs AUDITOR_JUDGE_BASE_URL, _API_KEY and _MODEL on the server)",
    {"threshold": 0.5}, default_enabled=False))


def evaluate_run(
    run: RunView,
    names: list[str] | None = None,
    params: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, ScoreResult | None], dict[str, str]]:
    """Run scorers on one run.

    Returns (results, errors). results maps scorer name -> ScoreResult, or None when the scorer
    does not apply. A scorer that raises is reported in errors and omitted from results, so a bug
    in a scorer is never mistaken for a failure of the agent.
    """
    registry = get_scorers()
    selected = names if names is not None else default_scorer_names()
    results: dict[str, ScoreResult | None] = {}
    errors: dict[str, str] = {}
    for name in selected:
        spec = registry.get(name)
        if spec is None:
            errors[name] = "unknown scorer"
            continue
        merged = {**spec.defaults, **((params or {}).get(name) or {})}
        try:
            results[name] = spec.fn(run, merged)
        except Exception as exc:  # noqa: BLE001 - scorers are untrusted-ish user code
            errors[name] = f"{type(exc).__name__}: {exc}"
    return results, errors
