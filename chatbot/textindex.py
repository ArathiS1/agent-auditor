"""A tiny keyword-and-weight (TF-IDF) search over text, used to find passages inside uploaded documents."""
from __future__ import annotations

import math
import re
from collections import Counter

STOP = set("a an the and or of to in on for with is are was were be been it its this that these those i you we my me do does did how what "
           "why when which who should can could would will about from as at by if so than then there their they them have has had not".split())


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9][a-z0-9\-_.]*[a-z0-9]|[a-z0-9]", text.lower()):
        if w in STOP:
            continue
        for suffix in ("ing", "ies", "ed", "es", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[: -len(suffix)] + ("y" if suffix == "ies" else "")
                break
        out.append(w)
    return out


def chunk(text: str, size: int = 1200) -> list[str]:
    """Split on paragraph breaks into pieces of about `size` characters."""
    pieces: list[str] = []
    current = ""
    for para in re.split(r"\n\s*\n|\n(?=\[page \d+\])", text):
        para = para.strip()
        if not para:
            continue
        while len(para) > size * 1.5:  # a very long paragraph: cut at a sentence end if possible
            cut = para.rfind(". ", 0, size) + 1 or size
            pieces.append(para[:cut].strip())
            para = para[cut:].strip()
        if len(current) + len(para) > size and current:
            pieces.append(current.strip())
            current = ""
        current += para + "\n\n"
    if current.strip():
        pieces.append(current.strip())
    return pieces


def top_passages(query: str, passages: list[str], k: int = 4) -> list[tuple[int, float]]:
    """Return (index, score) of the best passages for the query, best first."""
    docs = [Counter(tokens(p)) for p in passages]
    df: Counter[str] = Counter()
    for d in docs:
        df.update(d.keys())
    n = max(len(docs), 1)
    idf = {t: math.log((1 + n) / (1 + f)) + 1 for t, f in df.items()}
    q = Counter(t for t in tokens(query) if t in idf)
    scored = []
    for i, d in enumerate(docs):
        norm = math.sqrt(sum(((1 + math.log(c)) * idf[t]) ** 2 for t, c in d.items())) or 1.0
        score = sum((1 + math.log(c)) * idf[t] * ((1 + math.log(d[t])) * idf[t]) for t, c in q.items() if t in d) / norm
        if score > 0:
            scored.append((i, score))
    scored.sort(key=lambda x: -x[1])
    return scored[:k]
