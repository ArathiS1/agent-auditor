"""Canary comparison: judge a candidate version against the production baseline."""
from .checks import (
    CheckResult, Policy, check_causes, check_drift, check_evals, check_latency, check_replay,
)
from .decide import Decision, decide

__all__ = [
    "CheckResult", "Policy", "Decision", "decide", "check_evals", "check_drift", "check_latency",
    "check_causes", "check_replay",
]
