"""Remembers how each model has been behaving, so the chat can steer around slow or busy ones.

Every finished answer records how long it took; every retryable failure (rate limit, overload, timeout) puts the
model on a short cool-down. The fallback chain then prefers models that are quick and healthy. Nothing here
changes the agent itself, it only decides which model answers.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

SLOW_SECONDS = 40.0  # a model whose recent answers average longer than this is treated as slow
COOLDOWN = {429: 120.0, 503: 90.0, 500: 60.0, 502: 60.0, 504: 90.0, 0: 60.0}  # seconds to rest after each kind of failure
KEEP = 6  # how many recent answers count toward the average

_lock = threading.Lock()
_stats: dict[str, dict[str, Any]] = {}


def enabled(env: dict[str, str] | None = None) -> bool:
    return (env if env is not None else os.environ).get("CHAT_AUTOROUTE", "on").strip().lower() not in ("off", "0", "false", "no")


def _entry(mid: str) -> dict[str, Any]:
    return _stats.setdefault(mid, {"times": [], "fails": 0, "ok": 0, "until": 0.0, "last_status": 0})


def record_success(mid: str, seconds: float) -> None:
    with _lock:
        e = _entry(mid)
        e["times"] = (e["times"] + [max(0.0, seconds)])[-KEEP:]
        e["ok"] += 1
        e["fails"] = 0
        e["until"] = 0.0


def record_failure(mid: str, status: int = 0) -> None:
    with _lock:
        e = _entry(mid)
        e["fails"] += 1
        e["last_status"] = status
        rest = COOLDOWN.get(status, COOLDOWN[0]) * min(e["fails"], 4)  # repeated failures rest longer
        e["until"] = time.time() + rest


def cooling(mid: str, now: float | None = None) -> bool:
    with _lock:
        e = _stats.get(mid)
        return bool(e and e["until"] > (now if now is not None else time.time()))


def average(mid: str) -> float | None:
    with _lock:
        e = _stats.get(mid)
        if not e or not e["times"]:
            return None
        return sum(e["times"]) / len(e["times"])


def slow(mid: str) -> bool:
    avg = average(mid)
    return avg is not None and avg > SLOW_SECONDS


def avoid(mid: str) -> bool:
    """True when this model should not be tried first: resting after a failure, or reliably slow."""
    return cooling(mid) or slow(mid)


def sort_key(mid: str, provider: str = "") -> tuple[int, float]:
    """Lower is better: healthy models first, then by average time. Models with no history count as average speed."""
    avg = average(mid)
    return (1 if cooling(mid) else 0, 15.0 if avg is None else avg)


def reason(mid: str) -> str:
    if cooling(mid):
        with _lock:
            status = _stats[mid]["last_status"]
        return {429: "rate limited a moment ago", 503: "overloaded a moment ago"}.get(status, "failed a moment ago")
    avg = average(mid)
    if avg is not None and avg > SLOW_SECONDS:
        return f"has been slow lately (about {avg:.0f} s per answer)"
    return ""


def snapshot() -> list[dict[str, Any]]:
    now = time.time()
    with _lock:
        rows = []
        for mid, e in _stats.items():
            t = e["times"]
            rows.append({
                "model": mid,
                "answers": e["ok"],
                "average_seconds": round(sum(t) / len(t), 1) if t else None,
                "resting_for": max(0, round(e["until"] - now)),
                "last_status": e["last_status"],
            })
    return sorted(rows, key=lambda r: (r["resting_for"] > 0, r["average_seconds"] or 0))


def reset() -> None:
    with _lock:
        _stats.clear()
