"""Send a share of live traffic to a candidate version and record which arm handled each request.

    router = auditor.canary_router(
        baseline=("v1", answer_v1), candidate=("v2", answer_v2), fraction=0.1,
    )
    answer = router.call(user_id, question)       # same user_id -> same arm every time

Runs from the baseline arm are recorded in environment "production", runs from the candidate arm in
"canary", each with its own version label, so `auditor.evaluate_canary(...)` can compare them.

Honest limits
- A hash of the key gives a *sticky* split, not a randomised experiment. If the key correlates with
  something that affects quality (user tier, region, time of day) the arms are not comparable.
  Use a key that is effectively random per request (a request id) when you need an unbiased
  comparison, and a per-user key only when you need a user to always see the same version.
- If the candidate raises, the error is recorded and re-raised exactly as the baseline's would be.
  The router does not hide candidate failures or retry on the baseline; that choice is yours.
"""
from __future__ import annotations

import hashlib
import inspect
import random
import threading
from typing import Any, Callable

from .tracer import Auditor, _call_input, _replay_override

ARMS = ("baseline", "candidate")


class CanaryRouter:
    def __init__(
        self,
        auditor: Auditor,
        *,
        baseline: tuple[str, Callable],
        candidate: tuple[str, Callable],
        fraction: float = 0.1,
        salt: str = "",
        agent: str | None = None,
    ) -> None:
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("fraction must be between 0 and 1")
        for name, arm in (("baseline", baseline), ("candidate", candidate)):
            if not (isinstance(arm, tuple) and len(arm) == 2 and isinstance(arm[0], str) and callable(arm[1])):
                raise TypeError(f"{name} must be a (version_label, function) pair")
        if baseline[0] == candidate[0]:
            raise ValueError("baseline and candidate need different version labels")
        self._auditor = auditor
        self.arms = {"baseline": baseline, "candidate": candidate}
        self.fraction = fraction
        self.salt = salt
        self.agent = agent
        self._lock = threading.Lock()
        self._counts = {"baseline": 0, "candidate": 0}

    # -- assignment -------------------------------------------------------
    def choose(self, key: Any = None) -> str:
        """'baseline' or 'candidate'. The same key always gets the same arm (for a given salt)."""
        if self.fraction <= 0.0:
            return "baseline"
        if self.fraction >= 1.0:
            return "candidate"
        if key is None:
            share = random.random()
        else:
            digest = hashlib.sha256(f"{self.salt}:{key}".encode()).digest()
            share = int.from_bytes(digest[:8], "big") / 2**64
        return "candidate" if share < self.fraction else "baseline"

    def set_fraction(self, fraction: float) -> None:
        """Change the share of traffic (for example 0.05 -> 0.25). Sticky keys may change arm."""
        if not 0.0 <= fraction <= 1.0:
            raise ValueError("fraction must be between 0 and 1")
        self.fraction = fraction

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    # -- calling ----------------------------------------------------------
    def _begin(self, key: Any) -> tuple[str, str, Callable, dict[str, Any]]:
        arm = self.choose(key)
        label, fn = self.arms[arm]
        with self._lock:
            self._counts[arm] += 1
        override = {
            "environment": "canary" if arm == "candidate" else "production",
            "version": label, "claimed": False, "tags": {"canary_arm": arm},
        }
        return arm, label, fn, override

    def call(self, key: Any, *args: Any, **kwargs: Any) -> Any:
        """Run the chosen arm's function. Use `acall` for async functions."""
        _, _, fn, override = self._begin(key)
        reset = _replay_override.set(override)
        try:
            if getattr(fn, "_auditor_observe_run", False):
                return fn(*args, **kwargs)
            with self._auditor.run(input=_call_input(fn, args, kwargs), agent=self.agent) as run:
                result = fn(*args, **kwargs)
                run.output = result
                return result
        finally:
            _replay_override.reset(reset)

    async def acall(self, key: Any, *args: Any, **kwargs: Any) -> Any:
        _, _, fn, override = self._begin(key)
        reset = _replay_override.set(override)
        try:
            if getattr(fn, "_auditor_observe_run", False):
                return await _maybe_await(fn(*args, **kwargs))
            with self._auditor.run(input=_call_input(fn, args, kwargs), agent=self.agent) as run:
                result = await _maybe_await(fn(*args, **kwargs))
                run.output = result
                return result
        finally:
            _replay_override.reset(reset)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
