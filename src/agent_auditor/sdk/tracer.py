"""Agent Auditor SDK core.

Design rules
- Tracing must never break the host agent: every send path swallows and logs its own errors.
- Sending is non-blocking by default (background thread + bounded queue, drop-on-full).
- Framework-agnostic: works with plain Python; adapters (e.g. LangGraph) sit on top.

Typical use
    auditor = Auditor(agent="support-bot")
    auditor.register_version("v1", prompt=PROMPT, config={"top_k": 4})

    with auditor.run(input=question) as run:
        with run.step("kb_search", kind="retrieval", input=question) as s:
            docs = search(question)
            s.output = docs
            s.set(scores=[d.score for d in docs])
        run.output = answer
"""
from __future__ import annotations

import asyncio
import atexit
import contextvars
import functools
import inspect
import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote

from .serialize import to_jsonable

log = logging.getLogger("agent_auditor")

ENVIRONMENTS = ("production", "canary", "test")
STEP_KINDS = ("llm", "retrieval", "tool", "other")
MAX_STEPS = 1000  # server-side limit per run
_STOP = object()

_current_run: contextvars.ContextVar["Run | None"] = contextvars.ContextVar(
    "agent_auditor_current_run", default=None
)
# Set only while Auditor.replay_tests() calls the agent. Every run created in that scope is recorded
# in the "test" environment; the first one also carries the replay token, case id and version.
_replay_override: contextvars.ContextVar["dict[str, Any] | None"] = contextvars.ContextVar(
    "agent_auditor_replay_override", default=None
)


class AuditorError(Exception):
    """Raised only by explicit operations (e.g. register_version), never by tracing."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error_text(error: Any) -> str:
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    else:
        text = str(error)
    return text[:2000]


def _call_input(fn: Callable, args: tuple, kwargs: dict) -> Any:
    try:
        arguments = dict(inspect.signature(fn).bind(*args, **kwargs).arguments)
        arguments.pop("self", None)
        arguments.pop("cls", None)
        return arguments
    except Exception:
        return {"args": list(args), "kwargs": dict(kwargs)}


def _split_input(fn: Callable, value: Any) -> tuple[tuple, dict]:
    """Rebuild a call from a stored run input (see _call_input)."""
    if not isinstance(value, dict):
        return (value,), {}
    try:
        sig: inspect.Signature | None = inspect.signature(fn)
    except (TypeError, ValueError):  # no introspectable signature (some builtins)
        sig = None
    packed = set(value) == {"args", "kwargs"} and isinstance(value["args"], list) and isinstance(value["kwargs"], dict)
    if sig is None:
        return (tuple(value["args"]), dict(value["kwargs"])) if packed else ((), dict(value))
    variadic = any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in sig.parameters.values())
    try:
        sig.bind(**value)
        binds = True
    except TypeError:
        binds = False
    # {"args": [...], "kwargs": {...}} is how inputs of *args/**kwargs functions (and functions whose
    # signature could not be bound) are stored, so unpack it in those cases.
    if packed and (variadic or not binds):
        return tuple(value["args"]), dict(value["kwargs"])
    if binds:
        return (), dict(value)
    raise TypeError("the stored input does not match the agent function's parameters; pass call=")


async def _await(awaitable: Any) -> Any:
    return await awaitable


def _invoke(fn: Callable, value: Any, call: Callable | None) -> Any:
    if call is not None:
        result = call(value)
    else:
        args, kwargs = _split_input(fn, value)
        result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        result = asyncio.run(_await(result))
    return result


class Step:
    """One unit of work inside a run: an LLM call, retrieval, tool call, or anything else."""

    def __init__(
        self,
        name: str,
        kind: str = "other",
        input: Any = None,
        attributes: dict[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self.name = name
        self.attributes: dict[str, Any] = dict(attributes or {})
        if kind not in STEP_KINDS:
            self.attributes.setdefault("original_kind", kind)
            kind = "other"
        self.kind = kind
        self.input = input
        self.output: Any = None
        self.status = "ok"
        self.error: str | None = None
        self.started_at = started_at or _now()
        self.ended_at: datetime | None = None

    def set(self, **attributes: Any) -> "Step":
        self.attributes.update(attributes)
        return self

    def fail(self, error: Any) -> None:
        self.status = "error"
        self.error = _error_text(error)

    def end(self) -> None:
        if self.ended_at is None:
            self.ended_at = _now()

    def __enter__(self) -> "Step":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.fail(exc)
        self.end()
        return False

    def _payload(self) -> dict[str, Any]:
        ended = self.ended_at or _now()
        return {
            "kind": self.kind,
            "name": (self.name or "step")[:200],
            "input": to_jsonable(self.input),
            "output": to_jsonable(self.output),
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "ended_at": max(ended, self.started_at).isoformat(),
            "attributes": to_jsonable(self.attributes),
        }


class Run:
    """One end-to-end agent execution. Use as a context manager, or call finish() yourself."""

    def __init__(
        self,
        auditor: "Auditor",
        *,
        input: Any = None,
        tags: dict[str, Any] | None = None,
        environment: str | None = None,
        version: str | None = None,
        agent: str | None = None,
    ) -> None:
        override = _replay_override.get()
        if override is not None:
            environment = override["environment"]
            if not override["claimed"]:
                override["claimed"] = True
                if override["version"] is not None:
                    version = override["version"]
                tags = {**(tags or {}), **override["tags"]}
        env = environment or auditor.environment
        if env not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {ENVIRONMENTS}, got {env!r}")
        self._auditor = auditor
        self.agent = agent or auditor.agent
        self.environment = env
        self.version = version if version is not None else auditor.version
        self.input = input
        self.output: Any = None
        self.status = "ok"
        self.error: str | None = None
        self.tags: dict[str, Any] = dict(tags or {})
        self.steps: list[Step] = []
        self.started_at = _now()
        self.ended_at: datetime | None = None
        self._lock = threading.Lock()
        self._finished = False
        self._token: contextvars.Token | None = None

    # -- recording ---------------------------------------------------------
    def step(
        self, name: str, kind: str = "other", input: Any = None, attributes: dict[str, Any] | None = None
    ) -> Step:
        """Start a step. Steps are ordered by start time. Use as `with run.step(...) as s:`."""
        step = Step(name, kind=kind, input=input, attributes=attributes)
        with self._lock:
            self.steps.append(step)
        return step

    def add_step(
        self,
        name: str,
        *,
        kind: str = "other",
        input: Any = None,
        output: Any = None,
        status: str = "ok",
        error: Any = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Step:
        """Record a step after the fact (for work you did not wrap in a `with` block)."""
        step = self.step(name, kind=kind, input=input, attributes=attributes)
        if started_at is not None:
            step.started_at = started_at
        step.output = output
        if status == "error":
            step.fail(error if error is not None else "error")
        step.ended_at = ended_at or _now()
        return step

    def set_tags(self, **tags: Any) -> None:
        self.tags.update(tags)

    def fail(self, error: Any) -> None:
        self.status = "error"
        self.error = _error_text(error)

    def finish(self) -> None:
        """Close the run and queue it for sending. Safe to call more than once."""
        with self._lock:
            if self._finished:
                return
            self._finished = True
            steps = list(self.steps)
        self.ended_at = _now()
        try:
            for s in steps:
                s.end()
            payload = self._payload(steps)
        except Exception:
            log.exception("agent-auditor: could not build run payload; run dropped")
            return
        self._auditor._submit(payload)

    # -- context manager ---------------------------------------------------
    def __enter__(self) -> "Run":
        self._token = _current_run.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc is not None:
                self.fail(exc)
        finally:
            self.finish()
            if self._token is not None:
                try:
                    _current_run.reset(self._token)
                except ValueError:  # exited in a different context (e.g. async task)
                    _current_run.set(None)
                self._token = None
        return False

    def _payload(self, steps: list[Step]) -> dict[str, Any]:
        tags = dict(self.tags)
        if len(steps) > MAX_STEPS:
            tags["steps_truncated"] = len(steps) - MAX_STEPS
            steps = steps[:MAX_STEPS]
        ended = self.ended_at or _now()
        return {
            "agent": self.agent,
            "version": self.version,
            "environment": self.environment,
            "input": to_jsonable(self.input),
            "output": to_jsonable(self.output),
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "ended_at": max(ended, self.started_at).isoformat(),
            "tags": to_jsonable(tags),
            "steps": [s._payload() for s in steps],
        }


class Auditor:
    """Client for an Agent Auditor server."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        agent: str = "default",
        api_key: str | None = None,
        environment: str = "production",
        version: str | None = None,
        background: bool = True,
        max_queue: int = 1000,
        timeout: float = 5.0,
        http_client: Any = None,
        enabled: bool = True,
    ) -> None:
        if environment not in ENVIRONMENTS:
            raise ValueError(f"environment must be one of {ENVIRONMENTS}, got {environment!r}")
        self.agent = agent
        self.environment = environment
        self.version = version
        self.enabled = enabled
        self._api_key = api_key
        self._stats = {"sent": 0, "failed": 0, "dropped": 0}
        self._stats_lock = threading.Lock()
        self._closed = False
        self._owns_http = False
        self._http: Any = None
        self._queue: queue.Queue | None = None
        self._thread: threading.Thread | None = None

        if enabled:
            if http_client is not None:
                self._http = http_client
            else:
                import httpx  # imported lazily so the module works without it when disabled

                self._http = httpx.Client(base_url=base_url, timeout=timeout)
                self._owns_http = True
            if background:
                self._queue = queue.Queue(maxsize=max_queue)
                self._thread = threading.Thread(target=self._worker, name="agent-auditor-sender", daemon=True)
                self._thread.start()
        atexit.register(self._atexit)

    # -- public API --------------------------------------------------------
    @property
    def stats(self) -> dict[str, int]:
        with self._stats_lock:
            return dict(self._stats)

    def run(
        self,
        input: Any = None,
        *,
        tags: dict[str, Any] | None = None,
        environment: str | None = None,
        version: str | None = None,
        agent: str | None = None,
    ) -> Run:
        return Run(self, input=input, tags=tags, environment=environment, version=version, agent=agent)

    def current_run(self) -> Run | None:
        return _current_run.get()

    def register_version(
        self,
        label: str,
        prompt: str | None = None,
        config: dict[str, Any] | None = None,
        *,
        activate: bool = True,
        use: bool = True,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Register a prompt+config version on the server. Raises AuditorError on failure.

        With use=True (default) later runs from this Auditor are tagged with this version label.
        """
        if not self.enabled or self._http is None:
            return {}
        name = quote(agent or self.agent, safe="")
        body = {"label": label, "prompt": prompt, "config": to_jsonable(config or {}), "activate": activate}
        try:
            resp = self._http.post(f"/v1/agents/{name}/versions", json=body, headers=self._headers())
        except Exception as exc:
            raise AuditorError(f"could not reach the Auditor server: {exc}") from exc
        if resp.status_code >= 400:
            raise AuditorError(f"register_version failed: {resp.status_code} {resp.text[:300]}")
        data = resp.json()
        if use:
            self.version = data.get("label", label)
        return data

    def _api(self, method: str, path: str, *, json: Any = None, params: dict[str, Any] | None = None) -> Any:
        """Blocking call to the server for explicit operations. Raises AuditorError on failure."""
        if not self.enabled or self._http is None:
            raise AuditorError("this Auditor is disabled")
        try:
            if method == "POST":
                resp = self._http.post(path, json=json, headers=self._headers())
            else:
                resp = self._http.get(path, params=params, headers=self._headers())
        except Exception as exc:
            raise AuditorError(f"could not reach the Auditor server: {exc}") from exc
        if resp.status_code >= 400:
            raise AuditorError(f"{method} {path} failed: {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def run_evals(
        self,
        *,
        agent: str | None = None,
        version: str | None = None,
        environment: str | None = None,
        limit: int = 200,
        only_unevaluated: bool = True,
        scorers: list[str] | None = None,
        params: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Ask the server to score recent runs. Defaults to this Auditor's agent."""
        body: dict[str, Any] = {
            "agent": agent or self.agent, "version": version, "environment": environment,
            "limit": limit, "only_unevaluated": only_unevaluated, "params": params or {},
        }
        if scorers is not None:
            body["scorers"] = scorers
        return self._api("POST", "/v1/evals/run", json={k: v for k, v in body.items() if v is not None})

    def compare_versions(
        self,
        baseline: str,
        candidate: str,
        *,
        agent: str | None = None,
        baseline_environment: str | None = None,
        candidate_environment: str | None = None,
        min_samples: int = 20,
        min_effect: float = 0.05,
        alpha: float = 0.05,
    ) -> dict[str, Any]:
        """Compare two versions on eval pass rates. The result has an `overall_verdict`:
        regression, improvement, inconclusive, no_change or insufficient_data."""
        name = quote(agent or self.agent, safe="")
        params = {
            "baseline": baseline, "candidate": candidate, "min_samples": min_samples,
            "min_effect": min_effect, "alpha": alpha,
            "baseline_environment": baseline_environment, "candidate_environment": candidate_environment,
        }
        return self._api("GET", f"/v1/agents/{name}/compare", params={k: v for k, v in params.items() if v is not None})

    def check_drift(
        self,
        *,
        agent: str | None = None,
        environment: str = "production",
        version: str | None = None,
        current_hours: float = 24.0,
        reference_hours: float = 168.0,
        min_samples: int = 30,
        alpha: float = 0.01,
        include_evals: bool = True,
        save: bool = True,
    ) -> dict[str, Any]:
        """Ask the server whether the agent's recent behaviour differs from its baseline.

        The result has an `overall` verdict (stable, minor_drift, drift or insufficient_data) and a
        list of `signals`, each with a severity, the numbers behind it, and a plain-English reason.
        """
        body = {
            "agent": agent or self.agent, "environment": environment, "version": version,
            "current_hours": current_hours, "reference_hours": reference_hours, "min_samples": min_samples,
            "alpha": alpha, "include_evals": include_evals, "save": save,
        }
        return self._api("POST", "/v1/drift/check", json={k: v for k, v in body.items() if v is not None})

    # -- root-cause classification (Phase 6) ---------------------------------
    def diagnose(
        self,
        *,
        agent: str | None = None,
        environment: str = "production",
        version: str | None = None,
        since_hours: float | None = None,
        limit: int = 200,
        reclassify: bool = False,
    ) -> dict[str, Any]:
        """Classify the root cause of this agent's runs (newest first, runs without a diagnosis only)."""
        name = quote(agent or self.agent, safe="")
        body = {"environment": environment, "version": version, "since_hours": since_hours, "limit": limit, "reclassify": reclassify}
        return self._api("POST", f"/v1/agents/{name}/diagnose", json={k: v for k, v in body.items() if v is not None})

    def diagnose_run(self, run_id: str) -> dict[str, Any]:
        """Diagnose one run: the primary cause, how strong the evidence is, and every contributing finding."""
        return self._api("GET", f"/v1/runs/{quote(run_id, safe='')}/diagnosis")

    def root_causes(
        self,
        *,
        agent: str | None = None,
        environment: str = "production",
        version: str | None = None,
        since_hours: float | None = None,
        diagnose_first: bool = True,
        alpha: float = 0.01,
        min_runs: int = 20,
        min_count: int = 5,
        min_effect: float = 0.10,
    ) -> dict[str, Any]:
        """Why this agent fails: causes ranked by frequency (with the steps and sub-causes involved,
        example run ids and suggested first checks) and which causes cluster in one version."""
        name = quote(agent or self.agent, safe="")
        body = {
            "environment": environment, "version": version, "since_hours": since_hours, "diagnose_first": diagnose_first,
            "alpha": alpha, "min_runs": min_runs, "min_count": min_count, "min_effect": min_effect,
        }
        return self._api("POST", f"/v1/agents/{name}/root-causes", json={k: v for k, v in body.items() if v is not None})

    def root_cause_taxonomy(self) -> list[dict[str, Any]]:
        return self._api("GET", "/v1/root-causes/taxonomy")

    # -- canary comparison (Phase 7) -----------------------------------------
    def evaluate_canary(
        self,
        *,
        baseline: str,
        candidate: str,
        agent: str | None = None,
        baseline_environment: str = "production",
        candidate_environment: str = "canary",
        since_hours: float = 24.0,
        policy: dict[str, Any] | None = None,
        evaluate_first: bool = True,
        diagnose_first: bool = True,
        save: bool = True,
        scorers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Compare the candidate's recent runs with the baseline's and get promote / hold / rollback /
        continue, with every check that led there. `policy` overrides any threshold, for example
        {"min_canary_runs": 100, "require_replay": True}. `scorers` picks the checks run on unscored runs first
        (default: the built-in set); add "answer_judge" to have a model grade each answer."""
        name = quote(agent or self.agent, safe="")
        body = {
            "baseline": baseline, "candidate": candidate, "baseline_environment": baseline_environment,
            "candidate_environment": candidate_environment, "since_hours": since_hours,
            "evaluate_first": evaluate_first, "diagnose_first": diagnose_first, "save": save,
        }
        if policy:
            body["policy"] = policy
        if scorers is not None:
            body["scorers"] = list(scorers)
        return self._api("POST", f"/v1/agents/{name}/canary/evaluate", json=body)

    def list_canary_reports(self, *, agent: str | None = None, candidate: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        name = quote(agent or self.agent, safe="")
        params = {"limit": limit, "candidate": candidate}
        return self._api("GET", f"/v1/agents/{name}/canary", params={k: v for k, v in params.items() if v is not None})

    def get_canary_report(self, report_id: str) -> dict[str, Any]:
        return self._api("GET", f"/v1/canary/{quote(report_id, safe='')}")

    def canary_router(
        self,
        *,
        baseline: tuple[str, Callable],
        candidate: tuple[str, Callable],
        fraction: float = 0.1,
        salt: str = "",
        agent: str | None = None,
    ) -> Any:
        """Split traffic between two versions of the agent and record each arm; see sdk/canary.py."""
        from .canary import CanaryRouter

        return CanaryRouter(self, baseline=baseline, candidate=candidate, fraction=fraction, salt=salt, agent=agent)

    # -- releases: approval gates and rollback (Phase 8) ----------------------
    def propose_release(
        self, candidate: str, *, proposed_by: str, agent: str | None = None, notes: str | None = None,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ask to make `candidate` the active version. `policy` may set required_approvals,
        allow_self_approval, max_evidence_age_hours, require_replay, require_baseline_is_active."""
        name = quote(agent or self.agent, safe="")
        body: dict[str, Any] = {"candidate": candidate, "proposed_by": proposed_by}
        if notes:
            body["notes"] = notes
        if policy:
            body["policy"] = policy
        return self._api("POST", f"/v1/agents/{name}/releases", json=body)

    def approve_release(self, release_id: str, *, approver: str, comment: str | None = None) -> dict[str, Any]:
        return self._decide_release(release_id, approver, "approve", comment)

    def reject_release(self, release_id: str, *, approver: str, comment: str | None = None) -> dict[str, Any]:
        return self._decide_release(release_id, approver, "reject", comment)

    def _decide_release(self, release_id: str, approver: str, decision: str, comment: str | None) -> dict[str, Any]:
        body = {"approver": approver, "decision": decision, "comment": comment}
        return self._api("POST", f"/v1/releases/{quote(release_id, safe='')}/approve", json={k: v for k, v in body.items() if v is not None})

    def promote_release(self, release_id: str, *, actor: str) -> dict[str, Any]:
        """Activate the candidate. Raises AuditorError (HTTP 409) listing the blockers if the gate is not satisfied."""
        return self._api("POST", f"/v1/releases/{quote(release_id, safe='')}/promote", json={"actor": actor})

    def rollback_release(self, release_id: str, *, actor: str, reason: str, to_version: str | None = None) -> dict[str, Any]:
        body = {"actor": actor, "reason": reason, "to_version": to_version}
        return self._api("POST", f"/v1/releases/{quote(release_id, safe='')}/rollback", json={k: v for k, v in body.items() if v is not None})

    def cancel_release(self, release_id: str, *, actor: str, reason: str | None = None) -> dict[str, Any]:
        body = {"actor": actor, "reason": reason}
        return self._api("POST", f"/v1/releases/{quote(release_id, safe='')}/cancel", json={k: v for k, v in body.items() if v is not None})

    def get_release(self, release_id: str) -> dict[str, Any]:
        return self._api("GET", f"/v1/releases/{quote(release_id, safe='')}")

    def list_releases(self, *, agent: str | None = None, state: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        name = quote(agent or self.agent, safe="")
        params = {"state": state, "limit": limit}
        return self._api("GET", f"/v1/agents/{name}/releases", params={k: v for k, v in params.items() if v is not None})

    def release_audit(self, release_id: str) -> list[dict[str, Any]]:
        """Every recorded step of a release, oldest first."""
        return self._api("GET", f"/v1/releases/{quote(release_id, safe='')}/audit")

    def active_version(self, *, agent: str | None = None) -> dict[str, Any] | None:
        """The version production should run now (label, prompt, config), or None if none is active.
        Call this at startup or on a timer; promotion and rollback only take effect where something reads it."""
        name = quote(agent or self.agent, safe="")
        versions = self._api("GET", f"/v1/agents/{name}/versions")
        return next((v for v in versions if v.get("is_active")), None)

    # -- failure-to-test conversion and replay (Phase 5) --------------------
    def generate_tests(
        self,
        *,
        agent: str | None = None,
        environment: str = "production",
        since_hours: float | None = None,
        scorers: list[str] | None = None,
        limit: int = 50,
        include_weak: bool = False,
        similarity: float = 0.8,
        evaluate_first: bool = True,
        redact: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Turn this agent's failed production runs into regression test cases on the server.

        Repeated or near-identical failures are merged into one case. Use `dry_run=True` to preview
        and `redact=True` to mask emails, phone and card numbers before inputs are stored."""
        name = quote(agent or self.agent, safe="")
        body = {
            "environment": environment, "since_hours": since_hours, "scorers": scorers, "limit": limit,
            "include_weak": include_weak, "similarity": similarity, "evaluate_first": evaluate_first,
            "redact": redact, "dry_run": dry_run,
        }
        return self._api("POST", f"/v1/agents/{name}/tests/generate", json={k: v for k, v in body.items() if v is not None})

    def list_tests(self, *, agent: str | None = None, enabled: bool | None = None, limit: int = 100) -> list[dict[str, Any]]:
        name = quote(agent or self.agent, safe="")
        params: dict[str, Any] = {"limit": limit}
        if enabled is not None:
            params["enabled"] = "true" if enabled else "false"
        return self._api("GET", f"/v1/agents/{name}/tests", params=params)

    def test_from_run(
        self, run_id: str, *, name: str | None = None, expectations: list[dict[str, Any]] | None = None,
        redact: bool = False, notes: str | None = None,
    ) -> dict[str, Any]:
        """Pin one specific run as a regression test (derived from its failed evals unless you pass expectations)."""
        body = {"name": name, "expectations": expectations, "redact": redact, "notes": notes}
        return self._api("POST", f"/v1/runs/{quote(run_id, safe='')}/to-test", json={k: v for k, v in body.items() if v is not None})

    def get_replay(self, replay_id: str) -> dict[str, Any]:
        return self._api("GET", f"/v1/replays/{quote(replay_id, safe='')}")

    def replay_tests(
        self,
        fn: Callable,
        *,
        version: str | None = None,
        agent: str | None = None,
        case_ids: list[str] | None = None,
        limit: int | None = None,
        label: str | None = None,
        notes: str | None = None,
        call: Callable[[Any], Any] | None = None,
        flush_timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Run the agent on every enabled regression case and have the server grade the results.

        `fn` is the agent entry point; it is called with each stored input (a dict of arguments
        becomes keyword arguments). If `fn` is decorated with `observe_run`, its own run is
        recorded; otherwise this method records the run. Pass `call=lambda inp: ...` to adapt inputs
        yourself. Replay runs are stored in the "test" environment under `version`, so they never mix
        with production data. An exception in the agent is recorded as a failed run, not raised.
        The result has a `verdict` (pass, fail or incomplete) and one entry per case.
        """
        name = agent or self.agent
        quoted = quote(name, safe="")
        cases = self._api("GET", f"/v1/agents/{quoted}/tests", params={"enabled": "true", "limit": 500})
        if case_ids is not None:
            unknown = set(case_ids) - {c["id"] for c in cases}
            if unknown:
                raise ValueError(f"unknown or disabled test case ids: {', '.join(sorted(unknown)[:5])}")
            cases = [c for c in cases if c["id"] in set(case_ids)]
        if limit is not None:
            cases = cases[: max(0, limit)]
        if not cases:
            raise AuditorError("no enabled test cases to replay; call generate_tests() first")
        version = version if version is not None else self.version
        token = uuid.uuid4().hex
        before = self.stats
        client_errors: dict[str, str] = {}
        for case in cases:
            try:
                self._replay_one(fn, case, token=token, version=version, agent=name, call=call)
            except Exception as exc:  # noqa: BLE001 - a crashing agent is a test result, not a harness error
                client_errors[case["id"]] = f"{type(exc).__name__}: {exc}"[:300]
        flushed = self.flush(flush_timeout)
        after = self.stats
        body = {"version": version, "token": token, "case_ids": [c["id"] for c in cases], "label": label, "notes": notes}
        result = self._api("POST", f"/v1/agents/{quoted}/replays", json={k: v for k, v in body.items() if v is not None})
        result["flushed"] = flushed
        result["client_errors"] = client_errors
        result["runs_not_delivered"] = (after["failed"] - before["failed"]) + (after["dropped"] - before["dropped"])
        return result

    def _replay_one(self, fn: Callable, case: dict[str, Any], *, token: str, version: str | None, agent: str, call: Callable | None) -> None:
        override = {
            "environment": "test", "version": version, "claimed": False,
            "tags": {"replay_token": token, "case_id": case["id"]},
        }
        reset = _replay_override.set(override)
        try:
            if getattr(fn, "_auditor_observe_run", False):
                _invoke(fn, case.get("input"), call)
            else:
                with self.run(input=case.get("input"), agent=agent) as run:
                    run.output = _invoke(fn, case.get("input"), call)
        finally:
            _replay_override.reset(reset)

    def _call_in_test(self, fn: Callable, inp: Any, *, version: str | None, agent: str, call: Callable | None, tags: dict[str, Any]) -> Any:
        """Call the agent once, recorded in the 'test' environment under `version` (never mixes with production)."""
        reset = _replay_override.set({"environment": "test", "version": version, "claimed": False, "tags": tags})
        try:
            if getattr(fn, "_auditor_observe_run", False):
                return _invoke(fn, inp, call)
            with self.run(input=inp, agent=agent) as run:
                out = _invoke(fn, inp, call)
                run.output = out
                return out
        finally:
            _replay_override.reset(reset)

    # -- propose and verify fixes ------------------------------------------------
    def propose_fixes(
        self, *, agent: str | None = None, environment: str = "production", since_hours: float | None = None,
        base_version: str | None = None, use_ai: bool = False, max_candidates: int = 6,
    ) -> dict[str, Any]:
        """Ask the server for candidate fixes for this agent's failures. Nothing is changed or replayed."""
        body = {"environment": environment, "since_hours": since_hours, "base_version": base_version,
                "use_ai": use_ai, "max_candidates": max_candidates}
        return self._api("POST", f"/v1/agents/{quote(agent or self.agent, safe='')}/fixes/propose",
                         json={k: v for k, v in body.items() if v is not None})

    def verify_fixes(
        self, build: Callable[..., Callable], *, agent: str | None = None, environment: str = "production",
        since_hours: float | None = None, base_version: str | None = None, use_ai: bool = False, max_candidates: int = 4,
        guard: int = 20, guard_same: Callable[[Any, Any], bool] | None = None, call: Callable[[Any], Any] | None = None,
        min_fixed_share: float = 0.5,
    ) -> dict[str, Any]:
        """Propose fixes, replay each against the regression suite, and return only what passed.

        `build(prompt=..., config=...)` must return your agent function configured with that prompt and config.
        `guard` is how many recent answers that worked before the candidate must also handle without changing much.
        Needs regression cases (generate_tests) first. Activates nothing. See agent_auditor.sdk.fixes."""
        from .fixes import run_verification

        name = agent or self.agent
        if not self._api("GET", f"/v1/agents/{quote(name, safe='')}/tests", params={"enabled": "true", "limit": 1}):
            raise AuditorError("no regression cases yet; call generate_tests() first so fixes have failures to be proven against")
        proposal = self.propose_fixes(agent=name, environment=environment, since_hours=since_hours, base_version=base_version,
                                      use_ai=use_ai, max_candidates=max_candidates)
        guard_runs: list[dict[str, Any]] = []
        if guard > 0 and any(c.get("verifiable") for c in proposal["candidates"]):
            rows = self._api("GET", "/v1/runs", params={"agent": name, "status": "ok", "environment": environment,
                                                        "limit": min(max(guard, 1) * 3, 200)})
            guard_runs = [r for r in rows if r.get("input") is not None and r.get("output") is not None
                          and (base_version is None or r.get("version") == base_version)
                          and not (r.get("tags") or {}).get("guard")][:guard]
        return run_verification(self, build, agent=name, proposal=proposal, guard_runs=guard_runs, guard_same=guard_same,
                                call=call, min_fixed_share=min_fixed_share)

    def observe(
        self, name: str | None = None, *, kind: str = "other", capture_input: bool = True, capture_output: bool = True
    ) -> Callable:
        """Decorator: record the function as a step of the current run (no-op outside a run)."""

        def decorate(fn: Callable) -> Callable:
            step_name = name or fn.__name__

            def begin(args: tuple, kwargs: dict) -> Step | None:
                run = _current_run.get()
                if run is None:
                    return None
                return run.step(step_name, kind=kind, input=_call_input(fn, args, kwargs) if capture_input else None)

            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    step = begin(args, kwargs)
                    try:
                        result = await fn(*args, **kwargs)
                    except BaseException as exc:
                        if step is not None:
                            step.fail(exc)
                            step.end()
                        raise
                    if step is not None:
                        if capture_output:
                            step.output = result
                        step.end()
                    return result

                return async_wrapper

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                step = begin(args, kwargs)
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    if step is not None:
                        step.fail(exc)
                        step.end()
                    raise
                if step is not None:
                    if capture_output:
                        step.output = result
                    step.end()
                return result

            return wrapper

        return decorate

    def observe_run(
        self,
        name: str | None = None,
        *,
        tags: dict[str, Any] | None = None,
        capture_input: bool = True,
        capture_output: bool = True,
    ) -> Callable:
        """Decorator: record each call of the function as a whole run."""

        def decorate(fn: Callable) -> Callable:
            run_tags = {"function": name or fn.__name__, **(tags or {})}

            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                    inp = _call_input(fn, args, kwargs) if capture_input else None
                    with self.run(input=inp, tags=run_tags) as run:
                        result = await fn(*args, **kwargs)
                        if capture_output:
                            run.output = result
                        return result

                async_wrapper._auditor_observe_run = True  # type: ignore[attr-defined]
                return async_wrapper

            @functools.wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                inp = _call_input(fn, args, kwargs) if capture_input else None
                with self.run(input=inp, tags=run_tags) as run:
                    result = fn(*args, **kwargs)
                    if capture_output:
                        run.output = result
                    return result

            wrapper._auditor_observe_run = True  # type: ignore[attr-defined]
            return wrapper

        return decorate

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until queued runs are sent. Returns False if the timeout was hit first."""
        if self._queue is None:
            return True
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self, timeout: float = 10.0) -> None:
        if self._closed:
            return
        self.flush(timeout)
        self._closed = True
        if self._queue is not None:
            try:
                self._queue.put(_STOP, timeout=1.0)
            except queue.Full:
                pass
            if self._thread is not None:
                self._thread.join(timeout=1.0)
        if self._owns_http and self._http is not None:
            try:
                self._http.close()
            except Exception:
                pass

    def __enter__(self) -> "Auditor":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    # -- internals ---------------------------------------------------------
    def _headers(self) -> dict[str, str] | None:
        return {"X-API-Key": self._api_key} if self._api_key else None

    def _bump(self, key: str) -> None:
        with self._stats_lock:
            self._stats[key] += 1

    def _submit(self, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        if self._closed:
            self._bump("dropped")
            return
        if self._queue is None:
            self._send(payload)
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self._bump("dropped")
            log.warning("agent-auditor: send queue full; run dropped")

    def _send(self, payload: dict[str, Any]) -> None:
        try:
            resp = self._http.post("/v1/runs", json=payload, headers=self._headers())
            if resp.status_code >= 400:
                self._bump("failed")
                log.warning("agent-auditor: server rejected run (%s): %s", resp.status_code, resp.text[:300])
            else:
                self._bump("sent")
        except Exception as exc:
            self._bump("failed")
            log.warning("agent-auditor: could not send run: %s", exc)

    def _worker(self) -> None:
        assert self._queue is not None
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                self._send(item)
            except Exception:
                log.exception("agent-auditor: sender thread error")
            finally:
                self._queue.task_done()

    def _atexit(self) -> None:
        try:
            self.close(timeout=5.0)
        except Exception:
            pass
