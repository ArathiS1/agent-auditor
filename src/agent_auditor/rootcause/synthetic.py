"""Synthetic failures with a KNOWN injected cause, used to measure the classifier.

What this can and cannot tell you
- It checks that the rules do what they claim on a wide set of realistic error messages and run
  shapes, and it exposes where the evidence runs out (opaque messages, missing instrumentation).
- It does NOT measure accuracy on your production traffic. The generator and the classifier were
  written by the same people, so numbers here are an optimistic bound. The honest test is to compare
  diagnoses with incidents you have root-caused by hand.

Tiers
- clear:    the evidence is direct (standard error text, empty results, clearly low scores).
- noisy:    evidence is degraded (opaque messages, borderline scores, missing instrumentation, an
            unrelated error that merely mentions "timeout"). Some of these are expected misses.
- compound: two faults at once. The label follows the stated policy (an error beats a quality
            symptom; the earliest failing step wins; otherwise upstream quality symptoms win), so this
            tier checks that the policy is implemented, not that the policy is right.
- healthy:  normal runs, including near-threshold ones that must not be flagged.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from ..evals.types import RunView, StepView

TIERS = ("clear", "noisy", "compound", "healthy")
FAULTS = ("retrieval_error", "tool_timeout", "tool_error", "llm_error", "agent_exception", "retrieval_empty",
          "retrieval_low_relevance", "ungrounded_answer", "slow_run", "empty_output")

TIMEOUT_MSGS = (
    "TimeoutError: payments api timed out",
    "httpx.ReadTimeout: The read operation timed out",
    "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='api.stripe.com', port=443): Read timed out. (read timeout=10)",
    "asyncio.exceptions.TimeoutError",
    "grpc._channel._InactiveRpcError: StatusCode.DEADLINE_EXCEEDED: Deadline Exceeded",
    "psycopg2.errors.QueryCanceled: canceling statement due to statement timeout",
    "ETIMEDOUT: connect ETIMEDOUT 10.2.3.4:443",
    "HTTPError: 504 Gateway Timeout for url: https://crm.internal/v1/accounts",
)
TOOL_ERROR_MSGS = (
    "ConnectionError: HTTPSConnectionPool(host='crm.internal', port=443): Max retries exceeded with url: /v1/accounts "
    "(Caused by NewConnectionError('Failed to establish a new connection: [Errno 111] Connection refused'))",
    "HTTPError: 503 Server Error: Service Unavailable for url: https://orders.internal/v2/lookup",
    "KeyError: 'customer_id'",
    "ValueError: invalid literal for int() with base 10: 'N/A'",
    "PermissionError: 403 Forbidden for url: https://billing.internal/refunds",
    "json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
    "sqlalchemy.exc.OperationalError: server closed the connection unexpectedly",
    "HTTPError: 404 Client Error: Not Found for url: https://orders.internal/v2/orders/8841",
)
LLM_MSGS = (
    "openai.RateLimitError: Error code: 429 - {'error': {'message': 'Rate limit reached for gpt-4o in organization org-x'}}",
    "anthropic.APIStatusError: Error code: 529 - {'type': 'error', 'error': {'type': 'overloaded_error'}}",
    "openai.BadRequestError: This model's maximum context length is 8192 tokens. However, your messages resulted in 9120 tokens.",
    "openai.AuthenticationError: Error code: 401 - Incorrect API key provided",
    "openai.APIConnectionError: Connection error.",
    "openai.APITimeoutError: Request timed out.",
    "anthropic.InternalServerError: Error code: 500 - internal server error",
)
RETRIEVAL_MSGS = (
    "chromadb.errors.InvalidDimensionException: Embedding dimension 768 does not match collection dimensionality 1536",
    "qdrant_client.http.exceptions.ResponseHandlingException: Connection refused",
    "pinecone.exceptions.PineconeApiException: (401) Reason: Unauthorized",
    "weaviate.exceptions.WeaviateQueryError: Query call failed: index not found",
    "TimeoutError: vector search timed out after 5s",
)
AGENT_MSGS = (
    "ValueError: could not parse model output as JSON",
    "langchain_core.exceptions.OutputParserException: Could not parse LLM output: `I think the answer is`",
    "KeyError: 'answer'",
    "RecursionError: maximum recursion depth exceeded while calling a Python object",
    "TypeError: 'NoneType' object is not subscriptable",
)
# Messages that carry no signal, and ones that mislead a keyword matcher.
OPAQUE_MSGS = ("Exception", "failed", "unexpected error", "Error 17", "operation failed after 30s", "something went wrong", "None")
MISLEADING_TOOL_MSGS = ("ValueError: invalid timeout value: -1", "ConfigError: set request_timeout in settings")

WORDS = ("account", "invoice", "refund", "password", "subscription", "billing", "profile", "settings", "payment",
         "export", "workspace", "member", "security", "notification", "report", "domain", "template", "webhook")
OFF_WORDS = ("galaxy", "volcano", "orchestra", "saddle", "lantern", "glacier", "harbor", "violin", "meadow", "compass")


@dataclass(frozen=True)
class Case:
    run: RunView
    truth: str  # one of FAULTS or "healthy"
    tier: str


class _Builder:
    def __init__(self, rng: random.Random, i: int) -> None:
        self.rng = rng
        self.id = f"syn{i}"
        words = rng.sample(WORDS, 6)
        self.doc_words = words
        self.docs = [{"title": f"Help: {words[0]}", "text": " ".join(words[:4])}, {"title": f"Guide: {words[1]}", "text": " ".join(words[2:])}]
        self.steps: list[StepView] = []
        self.status, self.error, self.output = "ok", None, None
        self.latency = rng.uniform(400, 2500)
        self.input = f"How do I manage my {words[0]} and {words[1]}?"

    # -- step helpers
    def retrieval(self, *, docs=None, top=None, name="kb_search", status="ok", error=None, bare=False) -> StepView:
        docs = self.docs if docs is None else docs
        top = self.rng.uniform(0.65, 0.95) if top is None else top
        if bare:  # instrumentation gap: nothing recorded about what came back
            return StepView("retrieval", name, status, error, "q", None, {}, self.rng.uniform(40, 200))
        attrs = {"num_docs": len(docs), "scores": [round(top, 3)] if docs else []}
        return StepView("retrieval", name, status, error, "q", docs, attrs, self.rng.uniform(40, 200))

    def tool(self, name="lookup_account", status="ok", error=None) -> StepView:
        return StepView("tool", name, status, error, {"id": 1}, None if status == "error" else {"ok": True}, {}, self.rng.uniform(30, 300))

    def llm(self, status="ok", error=None, ms=None) -> StepView:
        return StepView("llm", "answer", status, error, "prompt", None if status == "error" else "text", {}, ms or self.rng.uniform(300, 1200))

    def grounded_answer(self) -> str:
        return "To manage that open " + " ".join(self.rng.sample(self.doc_words, 4)) + " from the menu"

    def build(self) -> RunView:
        return RunView(self.id, self.status, self.error, self.input, self.output, self.latency, tuple(self.steps))


def _healthy_base(b: _Builder, near_threshold: bool = False) -> None:
    top = b.rng.uniform(0.5, 0.56) if near_threshold else None
    b.steps = [b.retrieval(top=top), b.tool(), b.llm()]
    b.output = b.grounded_answer()
    if near_threshold:
        b.latency = b.rng.uniform(8500, 9800)


def _msg(b: _Builder, corpus: tuple[str, ...], tier: str, extra_opaque: bool = True) -> str | None:
    if tier == "clear":
        return b.rng.choice(corpus)
    roll = b.rng.random()
    if roll < 0.6 and extra_opaque:
        return b.rng.choice(OPAQUE_MSGS)
    if roll < 0.8:
        return None  # status says error, message is missing
    return b.rng.choice(corpus)


def _inject(b: _Builder, fault: str, tier: str) -> None:
    """Apply one fault to a healthy base run (mutating the builder)."""
    rng = b.rng
    noisy = tier == "noisy"
    if fault == "retrieval_error":
        msg = _msg(b, RETRIEVAL_MSGS, tier)
        b.steps[0] = b.retrieval(status="error", error=msg)
        b.status, b.error, b.output = "error", msg or "retrieval failed", None
    elif fault == "tool_timeout":
        if noisy:
            msg = rng.choice([None, "operation failed after 30s", "Error: no response", rng.choice(TIMEOUT_MSGS)])
        else:
            msg = rng.choice(TIMEOUT_MSGS)
        b.steps[1] = b.tool(status="error", error=msg)
        b.status, b.error, b.output = "error", msg or "tool failed", None
    elif fault == "tool_error":
        if noisy:
            msg = rng.choice([None, *OPAQUE_MSGS, *MISLEADING_TOOL_MSGS, rng.choice(TOOL_ERROR_MSGS)])
        else:
            msg = rng.choice(TOOL_ERROR_MSGS)
        b.steps[1] = b.tool(status="error", error=msg)
        b.status, b.error, b.output = "error", msg or "tool failed", None
    elif fault == "llm_error":
        msg = _msg(b, LLM_MSGS, tier)
        b.steps[2] = b.llm(status="error", error=msg)
        b.status, b.error, b.output = "error", msg or "llm failed", None
    elif fault == "agent_exception":
        msg = None if (noisy and rng.random() < 0.5) else rng.choice(AGENT_MSGS)
        b.status, b.error, b.output = "error", msg, None
    elif fault == "retrieval_empty":
        if noisy and rng.random() < 0.2:
            b.steps[0] = b.retrieval(bare=True)  # nothing recorded: expected to be missed
        else:
            b.steps[0] = b.retrieval(docs=[])
        b.output = "I could not find anything about that."
    elif fault == "retrieval_low_relevance":
        top = rng.uniform(0.40, 0.49) if noisy else rng.uniform(0.10, 0.35)
        b.steps[0] = b.retrieval(top=top)
        b.output = b.grounded_answer()
    elif fault == "ungrounded_answer":
        k = 1 if noisy else 0  # noisy: a little of the answer is supported
        b.output = "Certainly " + " ".join(rng.sample(OFF_WORDS, 5)) + " " + " ".join(rng.sample(b.doc_words, k))
    elif fault == "slow_run":
        b.latency = rng.uniform(10_500, 12_000) if noisy else rng.uniform(14_000, 40_000)
        slow = rng.choice((0, 1, 2))
        s = b.steps[slow]
        b.steps[slow] = StepView(s.kind, s.name, s.status, s.error, s.input, s.output, s.attributes, b.latency * rng.uniform(0.6, 0.9))
    elif fault == "empty_output":
        b.output = rng.choice(["", None, {}, []]) if not noisy else rng.choice(["   ", "\n", None])
    else:
        raise ValueError(fault)


# Pairs of faults and the label implied by the documented policy.
COMPOUND = (
    (("tool_timeout", "retrieval_empty"), "tool_timeout"),
    (("retrieval_empty", "slow_run"), "retrieval_empty"),
    (("llm_error", "retrieval_low_relevance"), "llm_error"),
    (("retrieval_error", "tool_error"), "retrieval_error"),
    (("tool_error", "llm_error"), "tool_error"),
    (("retrieval_low_relevance", "ungrounded_answer"), "retrieval_low_relevance"),
    (("agent_exception", "retrieval_empty"), "agent_exception"),
    (("slow_run", "empty_output"), "slow_run"),
)


def generate(seed: int = 0, per_cause: int = 40) -> list[Case]:
    rng = random.Random(seed)
    cases: list[Case] = []
    n = 0

    def fresh() -> _Builder:
        nonlocal n
        n += 1
        b = _Builder(rng, n)
        _healthy_base(b)
        return b

    for tier in ("clear", "noisy"):
        for fault in FAULTS:
            for _ in range(per_cause):
                b = fresh()
                _inject(b, fault, tier)
                cases.append(Case(b.build(), fault, tier))
    for (first, second), label in COMPOUND:
        for _ in range(per_cause):
            b = fresh()
            _inject(b, first, "clear")
            _inject(b, second, "clear")
            # Keep the "first" error as the earliest failing step by applying hard faults in step order.
            cases.append(Case(b.build(), label, "compound"))
    for i in range(per_cause * 5):
        n += 1
        b = _Builder(rng, n)
        _healthy_base(b, near_threshold=(i % 2 == 0))
        cases.append(Case(b.build(), "healthy", "healthy"))
    return cases
