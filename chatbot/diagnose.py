"""Tries every way of reaching your AI service and prints what each one really answers.

Run:  python chatbot/diagnose.py        (in the same window where the key is set)
"""
from __future__ import annotations


import httpx

from pathlib import Path

from envfile import load_env, problems

load_env(Path(__file__).resolve().parent)

import os  # noqa: E402
import sys  # noqa: E402

from providers import all_presets, resolve_provider  # noqa: E402


def _show(label: str, r: httpx.Response) -> None:
    body = r.text.strip().replace("\n", " ")[:300]
    print(f"[{'OK ' if r.status_code < 400 else 'FAIL'}] HTTP {r.status_code}  {label}\n       {body}\n")


def _tool_roundtrip(c: httpx.Client, p) -> None:
    """The step that fails in the chat: the model asks for a tool, we send the result back. Tries the variants."""
    url = f"{p.base_url}/chat/completions"
    h = {"authorization": f"Bearer {p.key}", **({"x-goog-api-key": p.key} if p.name == "gemini" else {})}
    tool = {"type": "function", "function": {"name": "calculator", "description": "Evaluate arithmetic",
            "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}}}
    first = [{"role": "user", "content": "What is 17*23? Use the calculator tool."}]
    r = c.post(url, json={"model": p.model, "messages": first, "tools": [tool]}, headers=h)
    _show("tool step 1: ask a question that needs the calculator", r)
    try:
        msg = r.json()["choices"][0]["message"]
        call = msg["tool_calls"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        print("The model did not ask for the tool, so the next steps are skipped.\n")
        return
    print("tool call keys:", sorted(call), "| extra_content present:", "extra_content" in call, "\n")
    result = {"role": "tool", "tool_call_id": call["id"], "content": "391"}
    plain = {k: v for k, v in call.items() if k != "extra_content"}
    variants = {
        "A: send the model's own message back unchanged": [*first, msg, result],
        "B: same, plus the tool name on the result": [*first, msg, {**result, "name": "calculator"}],
        "C: signature replaced by Google's placeholder": [*first, {**msg, "tool_calls": [{**plain, "extra_content": {"google": {"thought_signature": "skip_thought_signature_validator"}}}]}, result],
        "D: no signature at all": [*first, {**msg, "tool_calls": [plain]}, result],
    }
    for label, messages in variants.items():
        _show(f"tool step 2 - {label}", c.post(url, json={"model": p.model, "messages": messages, "tools": [tool]}, headers=h))


def key_table() -> None:
    print("Keys this app can see (values are never printed):")
    for name, pr in all_presets().items():
        if pr.key_env is None:
            continue
        k = pr.key
        state = "not set" if not k else ("looks like a placeholder, not a real key" if (pr.looks_like_placeholder or k.lower() == pr.name) else f"found, {len(k)} characters")
        print(f"  {pr.key_env:<22} {state}")
    for w in problems(Path(__file__).resolve().parent):
        print("  WARNING: " + w)
    print("  CHAT_PROVIDER in this window:", os.environ.get("CHAT_PROVIDER", "(not set)"), "| CHAT_MODEL:", os.environ.get("CHAT_MODEL", "(not set)"))
    print()


def main() -> None:
    key_table()
    if len(sys.argv) > 1:  # python chatbot\diagnose.py openrouter  tests that provider with its own default model
        env = {k: v for k, v in os.environ.items() if k not in ("CHAT_MODEL", "CHAT_PROVIDER")}
        p = resolve_provider({**env, "CHAT_PROVIDER": sys.argv[1]})
    else:
        p = resolve_provider()
    print(f"provider={p.name} model={p.model} key_length={len(p.key)} starts='{p.key[:4]}'\n")
    msg = [{"role": "user", "content": "Say hi"}]
    with httpx.Client(timeout=30) as c:
        if p.style == "openai":
            url = f"{p.base_url}/chat/completions"
            body = {"model": p.model, "messages": msg, "max_tokens": 20}
            auth = {"authorization": f"Bearer {p.key}"} if p.key else {}
            _show(f"POST {url}  (Bearer header)", c.post(url, json=body, headers=auth))
            if p.name == "gemini":
                _show(f"POST {url}  (x-goog-api-key header)", c.post(url, json=body, headers={"x-goog-api-key": p.key}))
                _show(f"POST {url}  (both headers)", c.post(url, json=body, headers={**auth, "x-goog-api-key": p.key}))
                base = "https://generativelanguage.googleapis.com/v1beta"
                native = {"contents": [{"parts": [{"text": "Say hi"}]}]}
                _show(f"POST {base}/models/{p.model}:generateContent  (native)",
                      c.post(f"{base}/models/{p.model}:generateContent", json=native, headers={"x-goog-api-key": p.key}))
                r = c.get(f"{base}/models", headers={"x-goog-api-key": p.key})
                try:
                    names = [m["name"] for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
                    print("Models your key can chat with:\n  " + "\n  ".join(names[:40]) + "\n")
                except (ValueError, AttributeError):
                    _show("list models", r)
        else:
            url = f"{p.base_url}/v1/messages"
            h = {"x-api-key": p.key, "anthropic-version": "2023-06-01"}
            _show(f"POST {url}", c.post(url, json={"model": p.model, "max_tokens": 20, "messages": msg}, headers=h))
        if p.style == "openai":
            _tool_roundtrip(c, p)
    print("Copy everything above into the chat if it still fails.")


if __name__ == "__main__":
    main()
