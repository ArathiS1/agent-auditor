"""Which AI service answers, and how to talk to it.

Claude (Anthropic) is the default. Anything that speaks the common "OpenAI-compatible" chat format also works,
which includes free options: Google Gemini (free key), Groq (free key), OpenRouter (free models), and Ollama (runs on your PC).
Messages are stored in one format (Claude's); this file converts them for the other services.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Iterator


class StreamError(RuntimeError):
    """The service answered 200 but put an error in the stream (gateways such as OpenRouter do this)."""


@dataclass(frozen=True)
class Provider:
    name: str
    style: str  # "anthropic" or "openai"
    base_url: str
    key_env: str | None  # None means no key is needed (Ollama)
    model: str
    tools: bool = True  # False for services that search the web themselves and refuse tool definitions (Perplexity)
    label: str = ""  # how people know it

    @property
    def key(self) -> str:
        """The key from the environment, with stray spaces, quotes and <> removed (a common copy-paste slip)."""
        return os.environ.get(self.key_env, "").strip().strip("\"'<>").strip() if self.key_env else ""

    @property
    def looks_like_placeholder(self) -> bool:
        k = self.key.lower()
        return bool(k) and (k in ("your key", "your_key", "your-key", "key", "api key") or "your" in k and " " in k or "..." in k)

    @property
    def ready(self) -> bool:
        return self.key_env is None or bool(self.key)


PRESETS: dict[str, Provider] = {
    "anthropic": Provider("anthropic", "anthropic", os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/"),
                          "ANTHROPIC_API_KEY", "claude-sonnet-5-5"),
    "gemini": Provider("gemini", "openai", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "gemini-flash-latest"),
    "groq": Provider("groq", "openai", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "openrouter": Provider("openrouter", "openai", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "meta-llama/llama-3.3-70b-instruct:free"),
    "ollama": Provider("ollama", "openai", "http://127.0.0.1:11434/v1", None, "llama3.1"),
    # more services to compare against (all speak the common OpenAI-style format). Set the key to turn each on.
    "openai": Provider("openai", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-5-mini", label="ChatGPT (OpenAI)"),
    "perplexity": Provider("perplexity", "openai", "https://api.perplexity.ai", "PERPLEXITY_API_KEY", "sonar", tools=False, label="Perplexity"),
    "deepseek": Provider("deepseek", "openai", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat", label="DeepSeek"),
    "mistral": Provider("mistral", "openai", "https://api.mistral.ai/v1", "MISTRAL_API_KEY", "mistral-small-latest", label="Mistral"),
    "cerebras": Provider("cerebras", "openai", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "llama3.1-8b", label="Cerebras"),
    # Indian prepaid gateways (pay with UPI); both speak the OpenAI format. Model names: run  python chatbot\\list_models.py airouter
    "airouter": Provider("airouter", "openai", "https://api.airouter.in/v1", "AIROUTER_API_KEY", "openai/gpt-5-nano", label="AIRouter (India)"),
    "nabh": Provider("nabh", "openai", "https://api.nabh.cloud/v1", "NABH_API_KEY", "mistral-7b-instruct", tools=False, label="nabh.cloud (India)"),
    "xai": Provider("xai", "openai", "https://api.x.ai/v1", "XAI_API_KEY", "grok-4", label="Grok (xAI)"),
}
_RESERVED = {"CHAT", "AUDITOR", "OPENAI", "ANTHROPIC", "TAVILY", "BRAVE", "OLLAMA"}


def all_presets(env: dict[str, str] | None = None) -> dict[str, Provider]:
    """The built-in services plus any OpenAI-compatible service you describe in .env:
         INDOS_API_KEY=...   INDOS_BASE_URL=https://api.example.com/v1   INDOS_MODEL=model-name
    (the three lines share one NAME; the base URL and model are required)."""
    env = os.environ if env is None else env
    out = dict(PRESETS)
    for k in sorted(env):
        if not k.endswith("_BASE_URL"):
            continue
        name = k[: -len("_BASE_URL")]
        low = name.lower()
        if not name or name in _RESERVED or low in out or not env.get(f"{name}_API_KEY", "").strip():
            continue
        base = env[k].strip().rstrip("/")
        model = env.get(f"{name}_MODEL", "").strip()
        if base.startswith(("http://", "https://")) and model:
            tools = env.get(f"{name}_TOOLS", "1").strip().lower() not in ("0", "false", "off", "no")  # NAME_TOOLS=off for models that cannot use tools
            out[low] = Provider(low, "openai", base, f"{name}_API_KEY", model, tools=tools, label=name.title())
    return out


LABELS = {"anthropic": "Claude (Anthropic)", "gemini": "Gemini (Google)", "groq": "Groq", "openrouter": "OpenRouter", "ollama": "Ollama (on this PC)"}


def resolve_provider(env: dict[str, str] | None = None) -> Provider:
    """CHAT_PROVIDER picks one. If it is not set, use whichever key is present (Claude first)."""
    env = os.environ if env is None else env
    name = env.get("CHAT_PROVIDER", "").strip().lower()
    if not name:
        name = next((n for n in ("anthropic", "gemini", "groq", "openrouter", "openai", "deepseek", "mistral", "xai") if env.get(PRESETS[n].key_env or "")), "anthropic")
    if name == "custom":  # any other OpenAI-compatible service
        base = env.get("CHAT_BASE_URL", "").rstrip("/")
        if not base:
            raise SystemExit("CHAT_PROVIDER=custom needs CHAT_BASE_URL (and usually CHAT_API_KEY and CHAT_MODEL).")
        key_env = "CHAT_API_KEY"
        preset = Provider("custom", "openai", base, key_env, env.get("CHAT_MODEL", ""))
    elif name in all_presets(env):
        preset = all_presets(env)[name]
    else:
        raise SystemExit(f"Unknown CHAT_PROVIDER '{name}'. Use one of: {', '.join([*PRESETS, 'custom'])}.")
    model = env.get("CHAT_MODEL") or preset.model
    if not model:
        raise SystemExit("Set CHAT_MODEL to the model name your provider offers.")
    return Provider(preset.name, preset.style, preset.base_url, preset.key_env, model, preset.tools, preset.label)


def display_name(p: Provider) -> str:
    return p.label or LABELS.get(p.name, p.name)


def provider_for(spec: str, env: dict[str, str] | None = None) -> Provider | None:
    """'provider:model' (or just 'provider') from the model picker. Only presets, never arbitrary URLs."""
    env = os.environ if env is None else env
    name, _, model = spec.partition(":")
    preset = all_presets(env).get(name.strip().lower())
    if preset is None:
        return None
    model = model.strip() or env.get(preset.name.upper() + "_MODEL", "") or preset.model
    if len(model) > 120 or not all(c.isalnum() or c in "-_.:/@" for c in model):
        return None
    return Provider(preset.name, preset.style, preset.base_url, preset.key_env, model, preset.tools, preset.label)


def available_models(env: dict[str, str] | None = None, include_unready: bool = False) -> list[dict[str, Any]]:
    """Models the person can pick: each preset whose key is set (or needs none), plus extras from CHAT_MODELS="gemini:gemini-3.5-flash,openai:gpt-5"."""
    env = os.environ if env is None else env
    specs: list[str] = []
    for name, preset in all_presets(env).items():
        if include_unready or preset.key_env is None or env.get(preset.key_env, "").strip():
            specs.append(name)
    specs += [x.strip() for x in env.get("CHAT_MODELS", "").split(",") if x.strip()]
    out, seen = [], set()
    for spec in specs:
        p = provider_for(spec, env)
        if p is None:
            continue
        ident = f"{p.name}:{p.model}"
        if ident in seen:
            continue
        seen.add(ident)
        ready = p.key_env is None or (bool(env.get(p.key_env, "").strip()) and not p.looks_like_placeholder
                                      and env.get(p.key_env, "").strip().lower() != p.name)
        out.append({"id": ident, "provider": p.name, "model": p.model, "name": display_name(p), "ready": ready, "tools": p.tools})
    return out


# ---------------------------------------------------------------- Claude format -> OpenAI format
def to_openai_tools(defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": d["name"], "description": d["description"], "parameters": d["input_schema"]}}
            for d in defs]


GEMINI_SKIP_SIGNATURE = "skip_thought_signature_validator"  # Google's documented placeholder for calls with no stored signature


def to_openai_messages(system: str, messages: list[dict[str, Any]], *, gemini: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in messages:
        c = m["content"]
        if m["role"] == "user":
            if isinstance(c, str):
                out.append({"role": "user", "content": c})
            elif c and c[0].get("type") == "tool_result":
                for b in c:
                    out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": str(b["content"])})
            else:  # a question with attached files / images
                parts: list[dict[str, Any]] = []
                for b in c:
                    if b.get("type") == "image":
                        src = b["source"]
                        parts.append({"type": "image_url", "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
                    else:
                        parts.append({"type": "text", "text": b["text"]})
                out.append({"role": "user", "content": parts})
        else:
            blocks = c if isinstance(c, list) else [{"type": "text", "text": c}]
            msg: dict[str, Any] = {"role": "assistant", "content": "".join(b.get("text", "") for b in blocks if b["type"] == "text")}
            calls = []
            for b in blocks:
                if b["type"] != "tool_use":
                    continue
                call = {"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
                if b.get("extra_content"):
                    call["extra_content"] = b["extra_content"]
                elif gemini:
                    call["extra_content"] = {"google": {"thought_signature": GEMINI_SKIP_SIGNATURE}}
                calls.append(call)
            if calls:
                msg["tool_calls"] = calls
                if not msg["content"]:
                    del msg["content"]  # Gemini accepts a tool call with no content field; an empty string can be refused
            out.append(msg)
    return out


# ---------------------------------------------------------------- OpenAI stream -> the same events the agent already understands
def parse_openai_stream(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Yield {"text": delta} events, then {"final": {content blocks, stop_reason, usage}}."""
    text = ""
    calls: dict[int, dict[str, Any]] = {}
    finish, usage = None, {}
    for line in lines:
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        if chunk.get("error"):
            raise StreamError(str(chunk["error"].get("message", chunk["error"]) if isinstance(chunk["error"], dict) else chunk["error"])[:300])
        if chunk.get("usage"):
            usage = {"input_tokens": chunk["usage"].get("prompt_tokens"), "output_tokens": chunk["usage"].get("completion_tokens")}
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                text += delta["content"]
                yield {"text": delta["content"]}
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                slot["id"] = tc.get("id") or slot["id"]
                if tc.get("extra_content"):  # Gemini 3: a thought signature that must be sent back with this call
                    slot["extra"] = tc["extra_content"]
                fn = tc.get("function") or {}
                slot["name"] = fn.get("name") or slot["name"]
                slot["args"] += fn.get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for n, i in enumerate(sorted(calls)):
        slot = calls[i]
        try:
            args = json.loads(slot["args"] or "{}")
        except ValueError:
            args = {}
        block = {"type": "tool_use", "id": slot["id"] or f"call_{n}", "name": slot["name"], "input": args if isinstance(args, dict) else {}}
        if slot.get("extra"):
            block["extra_content"] = slot["extra"]
        blocks.append(block)
    has_tools = any(b["type"] == "tool_use" for b in blocks)
    yield {"final": {"content": blocks, "stop_reason": "tool_use" if has_tools else "end_turn", "usage": usage}}


PREFERRED_FAMILIES = ("llama", "gemma", "qwen", "mistral", "deepseek", "gpt-oss", "glm", "nemotron", "hermes", "phi")


def openrouter_free_models(limit: int = 6) -> list[str]:
    """OpenRouter's free chat models right now that can use tools, well-known families first.
    (Some free models only work inside coding-agent apps; the chat skips those if they refuse.)"""
    try:
        import httpx

        data = httpx.get("https://openrouter.ai/api/v1/models", timeout=6).json().get("data", [])
    except Exception:  # noqa: BLE001 - discovery is a convenience
        return []
    free = [m for m in data if str(m.get("id", "")).endswith(":free") and "tools" in (m.get("supported_parameters") or [])
            and "audio" not in str(m.get("id", ""))]
    free.sort(key=lambda m: (not any(f in str(m["id"]).lower() for f in PREFERRED_FAMILIES), -int(m.get("context_length") or 0)))
    return [str(m["id"]) for m in free[:limit]]
