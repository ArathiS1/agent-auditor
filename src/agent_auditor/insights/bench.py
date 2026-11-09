"""Summarise benchmark runs: the same questions asked of several models, each answer checked against an expectation.

Pure functions (no database, no web) so the maths can be tested directly. The server turns stored runs into
`BenchRun` rows and calls `summarize`.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..evals.stats import wilson_interval
from ..rootcause.errors import error_kind

MIN_CATEGORY_N = 4


@dataclass
class BenchRun:
    run_id: str
    version: str
    target: str  # "provider:model", as written on the run
    question_id: str
    category: str
    question: str
    status: str  # ok | error
    error: str | None
    latency_ms: float | None
    passed: bool | None  # did the answer meet its expectation (None: no expectation was checked)
    reason: str | None
    answer: str
    tool_calls: int
    started_at: datetime
    feedback: str | None = None  # up | down, from a person
    extra: dict[str, Any] = field(default_factory=dict)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(p * (len(ordered) - 1))))
    return ordered[k]


def latest_per_question(runs: list[BenchRun]) -> list[BenchRun]:
    """One row per (version, question): the newest. Running a benchmark twice must not count twice."""
    best: dict[tuple[str, str], BenchRun] = {}
    for r in runs:
        key = (r.version, r.question_id)
        if key not in best or r.started_at > best[key].started_at:
            best[key] = r
    return list(best.values())


def _rate(passed: int, n: int) -> dict[str, Any]:
    lo, hi = wilson_interval(passed, n)
    return {"n": n, "passed": passed, "rate": (passed / n) if n else None, "low": lo if n else None, "high": hi if n else None}


def summarize(runs: list[BenchRun], current: str | None = None) -> dict[str, Any]:
    """Per target: pass rate with a 95% interval, per category, speed, errors, answer length; plus a question grid."""
    rows = latest_per_question(runs)
    by_version: dict[str, list[BenchRun]] = defaultdict(list)
    for r in rows:
        by_version[r.version].append(r)

    targets: list[dict[str, Any]] = []
    for version, items in by_version.items():
        judged = [r for r in items if r.passed is not None]
        passed = sum(1 for r in judged if r.passed)
        errors = [r for r in items if r.status == "error"]
        kinds: dict[str, int] = defaultdict(int)
        for r in errors:
            kinds[error_kind(r.error)] += 1
        lat = [r.latency_ms for r in items if r.latency_ms is not None and r.status == "ok"]
        words = [len(r.answer.split()) for r in items if r.status == "ok" and r.answer]
        cats: dict[str, dict[str, Any]] = {}
        for cat in sorted({r.category for r in judged}):
            sub = [r for r in judged if r.category == cat]
            cats[cat] = _rate(sum(1 for r in sub if r.passed), len(sub))
        ups = sum(1 for r in items if r.feedback == "up")
        downs = sum(1 for r in items if r.feedback == "down")
        targets.append({
            "version": version, "target": items[0].target, "current": version == current,
            "questions": len(items), "quality": _rate(passed, len(judged)),
            "errors": len(errors), "error_rate": len(errors) / len(items) if items else None,
            "error_kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
            "p50_ms": percentile(lat, 0.5), "p95_ms": percentile(lat, 0.95),
            "avg_words": (sum(words) / len(words)) if words else None,
            "tool_calls": sum(r.tool_calls for r in items), "categories": cats,
            "feedback": {"up": ups, "down": downs},
            "last_run": max(r.started_at for r in items).isoformat(),
        })
    targets.sort(key=lambda t: (-(t["quality"]["rate"] or 0), t["p50_ms"] or 1e12))

    grid: dict[str, dict[str, Any]] = {}
    for r in rows:
        cell = grid.setdefault(r.question_id, {"id": r.question_id, "category": r.category, "question": r.question, "results": {}})
        cell["results"][r.version] = {
            "passed": r.passed, "status": r.status, "error": (r.error or "")[:200] or None, "reason": r.reason,
            "answer": r.answer[:600], "latency_ms": r.latency_ms, "run_id": r.run_id, "tool_calls": r.tool_calls,
        }
    questions = sorted(grid.values(), key=lambda q: (q["category"], q["id"]))
    categories = sorted({q["category"] for q in questions})
    return {"targets": targets, "questions": questions, "categories": categories,
            "advice": advise(targets, current), "question_count": len(questions)}


# ---------------------------------------------------------------- advice
def advise(targets: list[dict[str, Any]], current: str | None) -> list[dict[str, Any]]:
    """Plain recommendations from the measured numbers. Each says what was seen, and what to try."""
    out: list[dict[str, Any]] = []
    if not targets:
        return out
    cur = next((t for t in targets if t["version"] == current), None)
    ranked = [t for t in targets if t["quality"]["n"]]
    best = ranked[0] if ranked else None

    def name(t: dict[str, Any]) -> str:
        return t["target"] or t["version"]

    def add(severity: str, area: str, title: str, detail: str, evidence: str) -> None:
        out.append({"severity": severity, "area": area, "title": title, "detail": detail, "evidence": evidence})

    if len(targets) < 2:
        add("info", "coverage", "Only one model has been benchmarked",
            "Comparisons need at least two. Run the benchmark with another provider, for example one of ChatGPT (OpenAI), Claude, Gemini or Perplexity.",
            f"{name(targets[0])} is the only target with results.")

    for t in targets:
        if t["questions"] >= 5 and t["error_rate"] and t["error_rate"] >= 0.10:
            kinds = ", ".join(f"{k} x{v}" for k, v in list(t["error_kinds"].items())[:3])
            fix = {"rate_limit": "Add retry with backoff, lower the request rate, or use a tier with more quota.",
                   "auth": "Check the API key and that it can use this model.",
                   "timeout": "Raise the timeout, or choose a faster model.",
                   "server_error": "The provider was unstable; retry later or add a fallback model.",
                   "context_length": "Shorten the inputs or choose a model with a longer context."}
            first = next(iter(t["error_kinds"]), "other")
            add("high", "reliability", f"{name(t)} failed on {t['errors']} of {t['questions']} questions",
                fix.get(first, "Open the failed runs to read the error text, then fix that cause first."), f"Error types: {kinds}.")

    if best and cur and best is not cur and cur["quality"]["n"]:
        gap = (best["quality"]["rate"] or 0) - (cur["quality"]["rate"] or 0)
        if gap >= 0.10 and best["quality"]["low"] > (cur["quality"]["high"] or 0) - 0.25:
            add("high" if gap >= 0.2 else "medium", "quality",
                f"{name(best)} answers {gap * 100:.0f} points more questions correctly than your current model",
                f"Run a canary of {name(best)} against {name(cur)} before switching; the release gate then needs approval.",
                f"{name(best)}: {best['quality']['passed']}/{best['quality']['n']}; {name(cur)}: {cur['quality']['passed']}/{cur['quality']['n']}.")
        for cat, c in (cur["categories"] or {}).items():
            b = (best["categories"] or {}).get(cat)
            if b and c["n"] >= MIN_CATEGORY_N and b["n"] >= MIN_CATEGORY_N and (b["rate"] - c["rate"]) >= 0.25:
                add("medium", "quality", f"Weak spot in '{cat}'",
                    f"Your current model passes {c['rate'] * 100:.0f}% here against {b['rate'] * 100:.0f}% for {name(best)}. "
                    "Try a clearer instruction in the system prompt for this kind of question, or route it to the stronger model.",
                    f"{c['passed']}/{c['n']} against {b['passed']}/{b['n']}.")

    if cur and cur["p95_ms"]:
        fastest = min((t for t in targets if t["p95_ms"]), key=lambda t: t["p95_ms"])
        if fastest is not cur and cur["p95_ms"] >= 2 * fastest["p95_ms"] and cur["p95_ms"] > 4000:
            add("medium", "speed", "Your current model is much slower than the fastest one",
                f"The slowest 5% of answers take {cur['p95_ms'] / 1000:.1f} s against {fastest['p95_ms'] / 1000:.1f} s for {name(fastest)}. "
                "Stream the answer (already on), shorten the prompt, or use a faster model for simple questions.",
                f"p95 {cur['p95_ms'] / 1000:.1f} s against {fastest['p95_ms'] / 1000:.1f} s.")

    words = [t["avg_words"] for t in targets if t["avg_words"]]
    if cur and cur["avg_words"] and len(words) >= 2:
        others = sorted(w for t in targets if t is not cur and t["avg_words"] for w in [t["avg_words"]])
        median = others[len(others) // 2] if others else None
        if median and cur["avg_words"] >= 2 * median:
            add("info", "style", "Your current model writes much longer answers than the others",
                "If people ask quick questions, add 'keep simple answers short' to the system prompt.",
                f"Average {cur['avg_words']:.0f} words against a median of {median:.0f} for the others.")

    for t in targets:
        calc = (t["categories"] or {}).get("tools")
        if calc and calc["n"] >= MIN_CATEGORY_N and calc["rate"] < 0.6 and t["tool_calls"] == 0:
            add("high", "tools", f"{name(t)} never used its tools",
                "It answered tool questions from memory. Check that this provider supports tool calling with the tool descriptions provided, "
                "or say 'use the calculator for any arithmetic' more firmly in the prompt.",
                f"Tools category {calc['passed']}/{calc['n']}, zero tool calls.")

    for t in targets:
        saf = (t["categories"] or {}).get("safety")
        if saf and saf["n"] >= MIN_CATEGORY_N and saf["rate"] < 0.75:
            add("high" if saf["rate"] < 0.5 else "medium", "safety", f"{name(t)} handled only {saf['passed']} of {saf['n']} safety questions as expected",
                "Read the failed answers: either it complied with a request it should decline, or it refused something harmless. Adjust the system prompt's rules.",
                f"Safety {saf['rate'] * 100:.0f}%.")

    if best and len(ranked) > 1:
        add("info", "summary", f"Best overall in this run: {name(best)}",
            f"{best['quality']['passed']} of {best['quality']['n']} answers met their expectation "
            f"(95% range {best['quality']['low'] * 100:.0f} to {best['quality']['high'] * 100:.0f}%)."
            + (" It is your current model." if best is cur else ""),
            "Small question sets give wide ranges; treat gaps under about 10 points as a tie.")
    order = {"high": 0, "medium": 1, "info": 2}
    out.sort(key=lambda a: order[a["severity"]])
    return out
