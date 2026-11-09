"""Read settings from a .env file so you do not have to type them in every new window.

Looks in chatbot/.env first, then the project's .env. Values already set in the window win, so you can
still override one for a single run. Only the settings this app uses are read (the other lines in a shared
.env, such as database passwords, are left alone).
"""
from __future__ import annotations

import os
from pathlib import Path

PREFIXES = ("CHAT_", "GEMINI_", "GROQ_", "OPENROUTER_", "ANTHROPIC_", "AUDITOR_", "TAVILY_", "BRAVE_",
            "OPENAI_", "PERPLEXITY_", "DEEPSEEK_", "MISTRAL_", "XAI_", "CEREBRAS_", "AIROUTER_", "NABH_")


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip().upper(), value.strip()  # Openrouter_API_KEY works too
        if value[:1] in "\"'" and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        elif " #" in value:  # an inline comment after an unquoted value
            value = value.split(" #", 1)[0].rstrip()
        if (key.startswith(PREFIXES) or key.endswith(("_API_KEY", "_BASE_URL", "_MODEL"))) and value and not key.startswith(("POSTGRES", "DATABASE")):
            out[key] = value
    return out


def load_env(here: Path) -> list[str]:
    """Set missing environment variables from .env files. Returns the files that were read."""
    used: list[str] = []
    for path in (here / ".env", here.parent / ".env"):
        try:
            values = parse(path.read_text(encoding="utf-8-sig"))
        except OSError:
            continue
        used.append(str(path))
        for key, value in values.items():
            if not os.environ.get(key, "").strip():  # a variable that exists but is empty counts as not set
                os.environ[key] = value
    return used


KNOWN_KEYS = ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "PERPLEXITY_API_KEY",
              "DEEPSEEK_API_KEY", "MISTRAL_API_KEY", "XAI_API_KEY", "CEREBRAS_API_KEY", "AIROUTER_API_KEY", "NABH_API_KEY", "TAVILY_API_KEY", "BRAVE_API_KEY", "AUDITOR_API_KEY")


def problems(here: Path) -> list[str]:
    """Plain-language warnings about the .env file: misspelled key names, empty values, window settings that override the file."""
    import difflib

    out: list[str] = []
    seen: set[str] = set()
    for path in (here / ".env", here.parent / ".env"):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            name = key.strip().upper().removeprefix("EXPORT ").strip()
            if name.endswith("_API_KEY") and name not in KNOWN_KEYS:
                stem = name[: -len("_API_KEY")]
                if f"{stem}_BASE_URL" in text.upper() and f"{stem}_MODEL" in text.upper():
                    continue  # a service you described yourself (name, base url and model)
                if f"{stem}_BASE_URL" in text.upper():
                    out.append(f"{name}: also add a line  {stem}_MODEL=<model name>  so the app can use it (in {path})")
                    continue
                near = difflib.get_close_matches(name, KNOWN_KEYS, n=1, cutoff=0.7)
                out.append(f"{name} is not a built-in name" + (f"; did you mean {near[0]}?" if near else ".")
                           + f" To add your own service, also set  {stem}_BASE_URL=https://.../v1  and  {stem}_MODEL=<model name>  (in {path})")
            elif name in KNOWN_KEYS and not value.strip().strip("\"'"):
                seen.add(name)  # present but empty: simply not enabled
            if name in ("CHAT_MODEL", "CHAT_PROVIDER") and value.strip().strip("\"'") and os.environ.get(name, "") not in ("", value.strip().strip("\"'")):
                out.append(f"{name} is set to '{os.environ.get(name)}' in this window, which overrides '{value.strip()}' in .env. "
                           f"Run  Remove-Item Env:{name}  or open a new terminal.")
    return out
