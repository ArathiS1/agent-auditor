"""Plain-text expectations about an answer, shared by regression tests and the benchmark.

An expectation is a small JSON object a person can read and edit:
  {"type": "output_contains", "value": "refund"}
  {"type": "output_not_contains", "value": "I cannot help"}
  {"type": "output_contains_any", "values": ["391", "three hundred ninety-one"]}
  {"type": "output_matches", "pattern": "^\\\\d{4}-\\\\d{2}-\\\\d{2}$"}
  {"type": "output_max_words", "value": 50}
  {"type": "output_min_words", "value": 20}
All checks are case-insensitive and look at the answer text only. No model is called.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

TEXT_KINDS = ("output_contains", "output_not_contains", "output_contains_any", "output_matches",
              "output_max_words", "output_min_words")
MAX_PATTERN = 200
MAX_VALUES = 20


def validate_text_expectation(i: int, e: Mapping[str, Any]) -> dict[str, Any]:
    """Return a cleaned copy of one text expectation, or raise ValueError saying what is wrong."""
    kind = e.get("type")
    why = str(e.get("why") or "")[:300]
    if kind in ("output_contains", "output_not_contains"):
        value = e.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"expectation {i}: 'value' must be a non-empty string")
        return {"type": kind, "value": value[:500], "why": why}
    if kind == "output_contains_any":
        values = e.get("values")
        if (not isinstance(values, list) or not values or len(values) > MAX_VALUES
                or not all(isinstance(v, str) and v.strip() for v in values)):
            raise ValueError(f"expectation {i}: 'values' must be a list of 1 to {MAX_VALUES} non-empty strings")
        return {"type": kind, "values": [v[:200] for v in values], "why": why}
    if kind == "output_matches":
        pattern = e.get("pattern")
        if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN:
            raise ValueError(f"expectation {i}: 'pattern' must be a regular expression of up to {MAX_PATTERN} characters")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"expectation {i}: bad pattern ({exc})") from exc
        return {"type": kind, "pattern": pattern, "why": why}
    if kind in ("output_max_words", "output_min_words"):
        value = e.get("value")
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100_000:
            raise ValueError(f"expectation {i}: 'value' must be a whole number of words")
        return {"type": kind, "value": value, "why": why}
    raise ValueError(f"expectation {i}: unknown type {kind!r}")


def check_text_expectation(e: Mapping[str, Any], text: str) -> tuple[bool, str]:
    """Does the answer text meet one text expectation? Returns (ok, plain reason)."""
    low = text.lower()
    kind = e.get("type")
    if kind == "output_contains":
        ok = str(e.get("value", "")).lower() in low
        return ok, f"answer {'contains' if ok else 'does not contain'} '{e.get('value')}'"
    if kind == "output_not_contains":
        ok = str(e.get("value", "")).lower() not in low
        return ok, f"answer {'avoids' if ok else 'contains'} '{e.get('value')}'"
    if kind == "output_contains_any":
        values = [str(v) for v in e.get("values", [])]
        hit = next((v for v in values if v.lower() in low), None)
        return hit is not None, (f"answer contains '{hit}'" if hit else "answer has none of: " + ", ".join(f"'{v}'" for v in values[:5]))
    if kind == "output_matches":
        ok = re.search(str(e.get("pattern", "")), text.strip(), re.IGNORECASE | re.DOTALL) is not None
        return ok, f"answer {'matches' if ok else 'does not match'} the pattern {e.get('pattern')}"
    words = len(text.split())
    if kind == "output_max_words":
        limit = int(e.get("value", 0))
        return words <= limit, f"answer has {words} words (limit {limit})"
    if kind == "output_min_words":
        limit = int(e.get("value", 0))
        return words >= limit, f"answer has {words} words (needs at least {limit})"
    raise ValueError(f"unknown expectation type {kind!r}")
