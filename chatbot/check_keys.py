"""Tests every API key in your .env with one tiny request each and says what is wrong with the ones that fail.

  python chatbot\\check_keys.py

Keys are never printed (only how long they are and the first few characters).
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from envfile import load_env, problems  # noqa: E402

load_env(Path(__file__).resolve().parent)

from agent import BILLING_WORDS, ChatAgent, is_model_only  # noqa: E402
from providers import all_presets, Provider, openrouter_free_models  # noqa: E402

PREFIX = {"gemini": ("AIza", "AQ."), "groq": ("gsk_",), "openrouter": ("sk-or-",), "anthropic": ("sk-ant-",), "openai": ("sk-",),
          "xai": ("xai-",), "deepseek": ("sk-",), "perplexity": ("pplx-",), "cerebras": ("csk-",), "airouter": ("sk-air-",)}


def verdict(p: Provider, status: int, body: str) -> tuple[str, str]:
    low = body.lower()
    if status == 200:
        return "OK", "works"
    if is_model_only(body) and status in (400, 401, 403, 404):
        return "MODEL REFUSED", "the key is fine; this particular model is not allowed for chat. Another model will work."
    if status == 402 or any(w in low for w in BILLING_WORDS):
        return "NO CREDIT", "the key is real but the account is empty. Add credit on the provider's billing page, or delete this key from .env."
    if status in (401, 403) or "api key" in low and "valid" in low or "user not found" in low or "no auth" in low:
        return "KEY REJECTED", "the provider does not accept this key. Create a new key and paste the WHOLE thing (no spaces or quotes)."
    if status == 429:
        return "OUT OF QUOTA", "the key works but its free quota or rate limit is used up. Wait, or use another provider."
    if status == 404:
        return "MODEL GONE", "the key may be fine, but this model name is not available to you. Pick another model."
    if status >= 500:
        return "PROVIDER BUSY", "the key may be fine; the provider is overloaded. Try again later."
    return f"HTTP {status}", body[:160]


def check(p: Provider, model: str) -> tuple[str, str]:
    try:
        with httpx.Client(timeout=60) as c:
            if p.style == "anthropic":
                r = c.post(f"{p.base_url}/v1/messages", headers={"x-api-key": p.key, "anthropic-version": "2023-06-01"},
                           json={"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "Say OK"}]})
            else:
                h = {"authorization": f"Bearer {p.key}"}
                r = c.post(f"{p.base_url}/chat/completions", headers=h,
                           json={"model": model, "max_tokens": 5, "messages": [{"role": "user", "content": "Say OK"}]})
    except httpx.TimeoutException:
        return "TIMED OUT", "no answer in 60 seconds. The provider is slow or busy; the key may be fine. Run this again in a few minutes."
    except httpx.HTTPError as exc:
        return "NO CONNECTION", f"could not reach {p.base_url}: {type(exc).__name__}. Check your internet connection or firewall."
    state, advice = verdict(p, r.status_code, r.text)
    if state != "OK":
        advice += f"\n{'':<51}provider says: {ChatAgent._detail(r.text)[:200] or r.text[:200]}"
    return state, advice


def main() -> int:
    for w in problems(Path(__file__).resolve().parent):
        print("WARNING: " + w)
    print(f"{'provider':<12}{'key':<24}{'result':<15}what to do")
    print("-" * 100)
    working = 0
    for name, pr in all_presets().items():
        if pr.key_env is None or not pr.key:
            continue
        shown = f"{len(pr.key)} chars, {pr.key[:5]}..."
        warn = ""
        pre = PREFIX.get(name)
        if pre and not pr.key.startswith(pre):
            warn = f" (expected to start with {' or '.join(pre)})"
        models = openrouter_free_models(4) or [pr.model] if name == "openrouter" else [pr.model]
        for model in models:  # OpenRouter: try a few free models; some refuse chat
            state, advice = check(pr, model)
            if state != "MODEL REFUSED":
                break
        if name == "openrouter":
            advice = f"[{model}] " + advice
        working += state == "OK"
        print(f"{name:<12}{shown:<24}{state:<15}{advice}{warn}")
    print("-" * 100)
    print(f"{working} provider(s) answered. 'OUT OF QUOTA', 'TIMED OUT', 'MODEL REFUSED' and 'PROVIDER BUSY' still count as a good key; 'KEY REJECTED' and 'NO CREDIT' do not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
