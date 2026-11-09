"""Tries several model names at one provider with a tiny request each, with and without a tool, and prints which ones work.

  python chatbot\\try_models.py indos Alibaba/qwen3.8-flash Deepseek/deepseek-v4-flash gemini/gemini-3.5-flash-lite

Each try costs a fraction of a paisa. "tools OK" means the model can be used with the chat's search and calculator;
"plain only" means add NAME_TOOLS=off for it (for example INDOS_TOOLS=off) or pick another one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from envfile import load_env  # noqa: E402

load_env(Path(__file__).resolve().parent)
from providers import all_presets  # noqa: E402

TOOL = [{"type": "function", "function": {"name": "calculator", "description": "Do arithmetic.",
                                         "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}}]


def ask(p, model: str, tools: bool) -> tuple[bool, str, float]:
    body = {"model": model, "max_tokens": 20, "messages": [{"role": "user", "content": "Reply with the word OK."}]}
    if tools:
        body["tools"] = TOOL
    try:
        r = httpx.post(p.base_url + "/chat/completions", json=body, headers={"authorization": f"Bearer {p.key}"}, timeout=90)
    except httpx.HTTPError as exc:
        return False, type(exc).__name__, 0.0
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:110]}".replace("\n", " "), r.elapsed.total_seconds()
    return True, "", r.elapsed.total_seconds()


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0].lower() not in all_presets():
        print("Usage: python chatbot\\try_models.py <provider> <model> [<model> ...]")
        return 2
    p = all_presets()[argv[0].lower()]
    if p.key_env and not p.key:
        print(f"{p.key_env} is not set in .env")
        return 2
    for m in argv[1:]:
        ok, why, secs = ask(p, m, False)
        if not ok:
            print(f"  FAILS      {m}   {why}")
            continue
        tok, twhy, _ = ask(p, m, True)
        print(f"  {'tools OK ' if tok else 'plain only'}  {m}   ({secs:.1f}s)" + ("" if tok else f"   tools: {twhy}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
