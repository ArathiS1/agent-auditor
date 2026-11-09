"""The agent under test: a help-center question answerer ("Northwind support bot").

What is real: the knowledge base, the retrieval (TF-IDF search), the answer, and the right/wrong judgement, which comes
from a known correct value for every question. What is simulated: step durations (drawn from fixed distributions so
runs are fast and repeatable) and, unless you pass --llm, the "model" (it returns the best passage). With --llm the
answer step calls the Claude API and its real duration is recorded.

A "version" is a Config. Faults are settings on a Config, so a deliberately worse v2 is one line.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

import kb

TIMEOUT_ERROR = "httpx.ReadTimeout: The read operation timed out"
RATE_ERROR = "openai.RateLimitError: Error code: 429 - Rate limit reached for requests"
DEFAULT_PROMPT = "You are a support assistant for Northwind Outfitters. Answer only from the help center text provided."
MIN_SCORE = 0.4  # below this the bot says it could not find the answer


@dataclass(frozen=True)
class Config:
    label: str
    prompt: str = DEFAULT_PROMPT
    top_k: int = 3
    timeout_rate: float = 0.0  # share of runs where the policy API call times out
    rate_limit_rate: float = 0.0  # share of runs where the model call is rate limited
    index_drop: float = 0.0  # share of help-center articles missing from the search index
    slow_rate: float = 0.0  # share of runs where the policy API is very slow
    stale_share: float = 0.0  # share of articles whose text is out of date (right article, wrong fact)
    seed: int = 0


@dataclass
class Truth:
    """What the harness knows about a run and the tool is never told."""
    question_id: str
    correct: bool
    effect: str | None  # tool_timeout | llm_error | slow_run | retrieval | stale | None
    in_scope: bool


@dataclass
class Record:
    payload: dict  # the body for POST /v1/runs
    truth: Truth
    run_id: str | None = None  # filled in after the server stores it
    diagnosis: dict | None = None  # the tool's root-cause answer for this run


class SupportBot:
    def __init__(self, cfg: Config, llm=None) -> None:
        self.cfg = cfg
        self.llm = llm  # optional callable (question, passages, prompt) -> str
        rng = random.Random(f"build|{cfg.label}|{cfg.seed}")
        ids = [f[0] for f in kb.FACTS]
        self.dropped = set(rng.sample(ids, round(len(ids) * cfg.index_drop)))
        self.stale = frozenset(rng.sample(ids, round(len(ids) * cfg.stale_share)))
        self.index = kb.Index([p for p in kb.passages(self.stale) if p.id not in self.dropped])

    def run(self, q: kb.Question, rng: random.Random, started: datetime, agent: str, environment: str) -> Record:
        cfg, now, steps = self.cfg, started, []

        def add(kind: str, name: str, ms: float, **kw) -> None:
            nonlocal now
            end = now + timedelta(milliseconds=ms)
            steps.append({"kind": kind, "name": name, "started_at": now.isoformat(), "ended_at": end.isoformat(), **kw})
            now = end

        found = self.index.search(q.text, cfg.top_k)
        add("retrieval", "kb_search", max(20, rng.gauss(60, 15)), input=q.text, output=[p.text for p, _ in found],
            attributes={"num_docs": len(found), "scores": [round(s, 3) for _, s in found], "top_k": cfg.top_k})

        slow = rng.random() < cfg.slow_rate
        timed_out = rng.random() < cfg.timeout_rate
        if timed_out:
            add("tool", "policy_api", 5000, status="error", error=TIMEOUT_ERROR)
        else:
            add("tool", "policy_api", max(900, rng.gauss(11500, 1500)) if slow else max(20, rng.gauss(80, 20)),
                output={"overrides": []})

        answer, status, error, effect = None, "ok", None, None
        if timed_out:
            status, error, effect = "error", TIMEOUT_ERROR, "tool_timeout"
        elif not found or found[0][1] < MIN_SCORE:
            answer = kb.DECLINE
        elif rng.random() < cfg.rate_limit_rate:
            add("llm", "answer", 200, status="error", error=RATE_ERROR)
            status, error, effect = "error", RATE_ERROR, "llm_error"
        else:
            t0 = datetime.now()
            answer = self.llm(q.text, [p.text for p, _ in found], cfg.prompt) if self.llm else found[0][0].text
            ms = (datetime.now() - t0).total_seconds() * 1000 if self.llm else max(80, rng.gauss(350, 80))
            add("llm", "answer", ms, input={"question": q.text}, output=answer)

        correct = (answer is not None) and ((q.gold is None and answer == kb.DECLINE) or (q.gold is not None and q.gold.lower() in answer.lower()))
        if effect is None and slow:
            effect = "slow_run"
        if effect is None and q.fact_id in self.dropped:
            effect = "retrieval"
        if effect is None and q.fact_id in self.stale:
            effect = "stale"
        payload = {
            "agent": agent, "version": cfg.label, "environment": environment, "status": status, "error": error,
            "input": {"question": q.text}, "output": answer, "tags": {"question_id": q.id},
            "started_at": started.isoformat(), "ended_at": now.isoformat(), "steps": steps,
        }
        return Record(payload, Truth(q.id, correct, effect, q.fact_id is not None))

    def traffic(self, n: int, rng: random.Random, start: datetime, span: timedelta, agent: str, environment: str) -> list[Record]:
        qs = kb.questions()
        gap = span / max(n, 1)
        return [self.run(rng.choice(qs), rng, start + gap * i, agent, environment) for i in range(n)]


def claude_answerer(api_key: str, model: str = "claude-sonnet-5-5"):
    """An answer function backed by the Claude API (used with --llm)."""
    from agent_auditor import assistant

    def answer(question: str, passages: list[str], prompt: str) -> str:
        context = "\n".join(f"- {p}" for p in passages)
        msg = f"Help center text:\n{context}\n\nQuestion: {question}\n\nIf the text does not answer it, reply exactly: {kb.DECLINE}"
        return assistant.complete(api_key=api_key, model=model, system=prompt, messages=[{"role": "user", "content": msg}], max_tokens=200)

    return answer
