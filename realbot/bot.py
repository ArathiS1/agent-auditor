"""A small notes question-answering bot (RAG), traced by Agent Auditor.

Two steps per question:
  1. search  - find the most relevant passages in your notes (TF-IDF, no extra libraries)
  2. answer  - Claude writes the answer from those passages (needs ANTHROPIC_API_KEY),
               or, with no key / --no-llm, the best matching sentence is returned instead.

Put your own notes (.md, .txt, .pdf) in realbot/notes/ and the bot answers from them.
Try it without the server:   python realbot/bot.py "What chunk size should I start with?"
"""
from __future__ import annotations

import math
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

DECLINE = "I could not find that in my notes."
HERE = Path(__file__).resolve().parent
API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
SCORE_SCALE = 2.4  # turns a raw cosine score into a 0-1 "how good is this match" number

SYSTEM = (
    "You answer questions using only the notes provided. "
    "If the notes do not contain the answer, reply exactly: " + DECLINE + " "
    "Otherwise answer in one or two short sentences and do not add facts that are not in the notes."
)

STOP = set(
    "a an the and or of to in on for with is are was were be been it its this that these those i you we my me do does did "
    "how what why when which who should can could would will about from as at by if so than then there their they them "
    "have has had not no yes use used using need needs".split()
)


def stem(word: str) -> str:
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def tokenize(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9][a-z0-9\-_.]*[a-z0-9]|[a-z0-9]", text.lower()) if w not in STOP]


@dataclass
class Chunk:
    id: str
    source: str
    title: str
    text: str


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def load_chunks(folder: Path) -> list[Chunk]:
    """Read every .md / .txt / .pdf file under `folder`; one chunk per paragraph (headings become the title)."""
    chunks: list[Chunk] = []
    for path in sorted(folder.rglob("*")):
        if path.suffix.lower() not in (".md", ".txt", ".pdf") or not path.is_file():
            continue
        if path.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader  # optional: pip install pypdf
            except ImportError:
                print(f"skipping {path.name}: install pypdf to read PDFs (pip install pypdf)", file=sys.stderr)
                continue
            text = "\n\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
        else:
            text = path.read_text(encoding="utf-8", errors="ignore")
        title = path.stem
        for n, para in enumerate(_paragraphs(text)):
            if para.startswith("#"):
                title = para.lstrip("#").strip().splitlines()[0]
                continue
            if len(para) < 20:
                continue
            chunks.append(Chunk(f"{path.stem}#{n}", path.name, title, para))
    return chunks


class Index:
    """TF-IDF over chunks. Search returns (chunk, score) with score in 0..1."""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        docs = [tokenize(c.title + " " + c.text) for c in chunks]
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        n = max(len(docs), 1)
        self.idf = {t: math.log((1 + n) / (1 + f)) + 1 for t, f in df.items()}
        self.vectors = [self._vec(d) for d in docs]

    def _vec(self, tokens: list[str], query: bool = False) -> dict[str, float]:
        tf = Counter(tokens)
        # Words the notes never use count against a question (they dilute the match), so "tell me a joke"
        # cannot score highly just because one common word appears somewhere in the notes.
        rare = max(self.idf.values(), default=1.0)
        vec = {t: (1 + math.log(c)) * self.idf.get(t, rare if query else 0.0) for t, c in tf.items()
               if query or t in self.idf}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items() if t in self.idf}

    def search(self, query: str, top_k: int = 4) -> list[tuple[Chunk, float]]:
        q = self._vec(tokenize(query), query=True)
        if not q:
            return []
        scored = []
        for chunk, vec in zip(self.chunks, self.vectors):
            cos = sum(w * vec.get(t, 0.0) for t, w in q.items())
            if cos > 0:
                scored.append((chunk, min(1.0, cos * SCORE_SCALE)))
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]


class RateLimitError(Exception):
    """Stands in for a provider rate-limit error when --break llm simulates an outage."""


@dataclass
class BotConfig:
    top_k: int = 4
    min_score: float = 0.35
    use_llm: bool = False
    model: str = DEFAULT_MODEL
    break_mode: str | None = None  # None | "search" | "llm": simulate a bad release or outage, for testing the auditor
    break_rate: float = 0.35
    seed: int | None = None

    def describe(self) -> dict[str, Any]:
        return {"top_k": self.top_k, "min_score": self.min_score, "answerer": self.model if self.use_llm else "extractive",
                "break": self.break_mode, "break_rate": self.break_rate if self.break_mode else 0}


class _NoSpan:
    """Stand-in used when the bot runs without tracing."""

    output: Any = None

    def __enter__(self) -> "_NoSpan":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def set(self, **_: Any) -> None:
        pass


@dataclass
class NotesBot:
    notes_dir: Path = HERE / "notes"
    cfg: BotConfig = field(default_factory=BotConfig)
    auditor: Any = None  # an agent_auditor.sdk.Auditor, or None to run untraced
    transport: Any = None  # tests inject an httpx MockTransport here

    def __post_init__(self) -> None:
        self.index = Index(load_chunks(Path(self.notes_dir)))
        if not self.index.chunks:
            raise SystemExit(f"No notes found in {self.notes_dir}. Add .md, .txt or .pdf files there.")
        self.rng = random.Random(self.cfg.seed)
        self._http = httpx.Client(timeout=30, transport=self.transport) if self.cfg.use_llm else None

    # ---- the two steps -------------------------------------------------
    def search(self, question: str) -> list[tuple[Chunk, float]]:
        if self.cfg.break_mode == "search" and self.rng.random() < self.cfg.break_rate:
            return []  # simulated broken index
        return self.index.search(question, self.cfg.top_k)

    def generate(self, question: str, hits: list[tuple[Chunk, float]]) -> str:
        if self.cfg.break_mode == "llm" and self.rng.random() < self.cfg.break_rate:
            raise RateLimitError("Error code: 429 - Rate limit reached for requests")
        if self.cfg.use_llm:
            return self._claude(question, hits)
        return self._extract(question, hits[0][0])

    def _extract(self, question: str, chunk: Chunk) -> str:
        wanted = set(tokenize(question))
        best, best_score = chunk.text, -1.0
        for sentence in re.split(r"(?<=[.!?])\s+", chunk.text):
            score = sum(self.index.idf.get(t, 0.0) for t in set(tokenize(sentence)) & wanted)
            if score > best_score:
                best, best_score = sentence, score
        return best

    def _claude(self, question: str, hits: list[tuple[Chunk, float]]) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
        notes = "\n\n".join(f"[{i + 1}] ({c.source}) {c.text}" for i, (c, _) in enumerate(hits))
        body = {"model": self.cfg.model, "max_tokens": 300, "temperature": 0, "system": SYSTEM,
                "messages": [{"role": "user", "content": f"Notes:\n{notes}\n\nQuestion: {question}"}]}
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        for attempt in range(3):
            resp = self._http.post(API_URL, json=body, headers=headers)
            if resp.status_code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Claude API returned HTTP {resp.status_code}: {resp.text[:200]}")
            return "".join(b.get("text", "") for b in resp.json().get("content", []) if b.get("type") == "text").strip()
        raise RuntimeError("Claude API did not answer")

    # ---- one question, traced -------------------------------------------
    def ask(self, question: str, **run_options: Any) -> str:
        if self.auditor is None:
            return self._answer(question, None)
        with self.auditor.run(input=question, **run_options) as run:
            answer = self._answer(question, run)
            run.output = answer
            return answer

    def _answer(self, question: str, run: Any) -> str:
        def span(name: str, kind: str) -> Any:
            return run.step(name, kind=kind, input=question) if run is not None else _NoSpan()

        with span("search", "retrieval") as s:
            hits = self.search(question)
            s.output = [{"source": c.source, "text": c.text[:160], "score": round(sc, 3)} for c, sc in hits]
            s.set(scores=[round(sc, 3) for _, sc in hits], num_docs=len(hits))
        good = [(c, sc) for c, sc in hits if sc >= self.cfg.min_score]
        if not good:
            return DECLINE
        with span("answer", "llm") as s:
            reply = self.generate(question, good)
            s.output = reply
        return reply


def build_bot(*, notes: Path | None = None, auditor: Any = None, use_llm: bool | None = None, **options: Any) -> NotesBot:
    if use_llm is None:
        use_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if use_llm and not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set. Set it, or run with --no-llm.")
    cfg = BotConfig(use_llm=use_llm, model=os.environ.get("BOT_MODEL", DEFAULT_MODEL), **options)
    return NotesBot(notes_dir=notes or HERE / "notes", cfg=cfg, auditor=auditor)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit('Usage: python realbot/bot.py "your question"')
    bot = build_bot()
    q = " ".join(sys.argv[1:])
    print(f"[{'Claude' if bot.cfg.use_llm else 'extractive'} answerer]")
    for chunk, score in bot.search(q):
        print(f"  match {score:.2f}  {chunk.source}")
    print(bot.ask(q))
