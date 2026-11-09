from __future__ import annotations

from pathlib import Path


def _load_dotenv() -> None:
    """Let the server see the AI keys in the project's .env (the run script exports only some of them).
    Values already set in the window win. Only the known setting names are read."""
    prefixes = ("ANTHROPIC_", "GEMINI_", "GROQ_", "OPENROUTER_", "OPENAI_", "DEEPSEEK_", "MISTRAL_", "XAI_", "CEREBRAS_", "AIROUTER_", "NABH_")
    import os

    for path in (Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env"):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            key, value = key.strip().upper(), value.strip()
            if value[:1] in "\"'" and value[-1:] == value[:1] and len(value) >= 2:
                value = value[1:-1]
            if (key.startswith(prefixes) or key.endswith(("_API_KEY", "_BASE_URL", "_MODEL"))) and value and key != "AUDITOR_API_KEY":
                os.environ.setdefault(key, value)


_load_dotenv()

from .app import create_app  # noqa: E402

app = create_app()
