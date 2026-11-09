"""Pure helpers for turning production failures into stable, de-duplicated test cases:
input fingerprints, near-duplicate detection, failure signatures and best-effort redaction.
No database or web imports, so everything here is testable without a server."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from ..evals.scorers import _extract_text, _tokens
from ..evals.types import RunView

_WS = re.compile(r"\s+")
_MAX_DEPTH = 8


# ---------------------------------------------------------------- fingerprints
def normalize_input(value: Any, _depth: int = 0) -> Any:
    """Canonical form of a run input: strings lower-cased and whitespace-collapsed, keys as strings."""
    if _depth > _MAX_DEPTH:
        return "..."
    if isinstance(value, str):
        return _WS.sub(" ", value.strip().lower())
    if isinstance(value, dict):
        return {str(k): normalize_input(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_input(v, _depth + 1) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def fingerprint(value: Any) -> str:
    """Stable hash of a run input. Equal after normalisation means the same test."""
    text = json.dumps(normalize_input(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def input_tokens(value: Any) -> frozenset[str]:
    pieces: list[str] = []
    _extract_text(value, pieces, [20_000])
    return frozenset(_tokens(" ".join(pieces)))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def is_near_duplicate(a: frozenset[str], b: frozenset[str], threshold: float = 0.8, min_tokens: int = 2) -> bool:
    """Two inputs are near-duplicates when their content-word sets overlap strongly. Very short
    inputs (fewer than `min_tokens` content words) only match when identical."""
    if len(a) < min_tokens or len(b) < min_tokens:
        return a == b and bool(a)
    return jaccard(a, b) >= threshold


# ---------------------------------------------------------------- failure signature
def failure_info(run: RunView, failed: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """What went wrong on a run, in a compact form that later phases (root cause) can read."""
    failed = list(failed)
    error = (run.error or "")[:300] or None
    error_type = None
    if error and ":" in error.split("\n", 1)[0]:
        head = error.split(":", 1)[0].strip()
        if head and len(head) <= 80 and " " not in head:
            error_type = head
    return {
        "scorers": sorted({str(f["scorer"]) for f in failed}),
        "reasons": {str(f["scorer"]): str(f.get("reason") or "")[:300] for f in failed},
        "error": error,
        "error_type": error_type,
        "failed_steps": sorted({s.name for s in run.steps if s.status == "error"}),
    }


def case_name(failure: Mapping[str, Any], input_value: Any, max_len: int = 90) -> str:
    pieces: list[str] = []
    _extract_text(input_value, pieces, [200])
    snippet = _WS.sub(" ", " ".join(pieces)).strip()
    if len(snippet) > 60:
        snippet = snippet[:57] + "..."
    label = "+".join(failure.get("scorers") or ["failure"])
    return f"{label}: {snippet}"[:max_len] if snippet else label[:max_len]


# ---------------------------------------------------------------- de-duplication
def plan_cases(candidates: list[dict[str, Any]], known: list[dict[str, Any]], threshold: float = 0.8):
    """Decide which candidate failures become new cases and which merge into an existing one.

    candidates: dicts with `key`, `fingerprint`, `tokens`, `scorers` (frozenset), newest first.
    known:      dicts with the same fields, for cases that already exist.
    Returns (new, merged) where `new` is a list of candidates and `merged` is a list of
    (candidate, known_key) pairs. A candidate is a duplicate when its fingerprint equals a known
    one, or when its tokens are a near-duplicate AND it failed the same scorers. Accepted new
    candidates join the pool, so later candidates de-duplicate against them too.
    """
    pool = list(known)
    by_fp = {k["fingerprint"]: k["key"] for k in pool}
    new: list[dict[str, Any]] = []
    merged: list[tuple[dict[str, Any], Any]] = []
    for cand in candidates:
        hit = by_fp.get(cand["fingerprint"])
        if hit is None:
            for k in pool:
                if k["scorers"] == cand["scorers"] and is_near_duplicate(cand["tokens"], k["tokens"], threshold):
                    hit = k["key"]
                    break
        if hit is not None:
            merged.append((cand, hit))
        else:
            new.append(cand)
            pool.append(cand)
            by_fp[cand["fingerprint"]] = cand["key"]
    return new, merged


# ---------------------------------------------------------------- redaction (best effort)
_PATTERNS = (
    ("SECRET", re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b|\bBearer\s+[A-Za-z0-9._~+/-]{16,}|\b[A-Fa-f0-9]{32,}\b")),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")),
)


def redact_text(text: str) -> str:
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[{label}]", text)
    return text


def redact_value(value: Any, _depth: int = 0) -> Any:
    """Mask emails, phone numbers, card-like numbers, SSNs and key-like secrets in nested data.
    Best effort only: names, addresses and free-form personal details are NOT detected."""
    if _depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, _depth + 1) for v in value]
    return value
