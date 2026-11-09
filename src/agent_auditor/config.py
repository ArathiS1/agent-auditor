from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    api_key: str | None
    auto_create_tables: bool = True
    anthropic_api_key: str | None = None  # enables the dashboard helper and suggested fixes
    assistant_model: str = "claude-sonnet-5-5"


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    text = value.strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"expected true or false, got {value!r}")


def get_settings() -> Settings:
    """Read settings from the environment.

    AUDITOR_DATABASE_URL  defaults to a local SQLite file (use a postgresql+psycopg URL in production)
    AUDITOR_API_KEY       if set, every /v1 request must send it in the X-API-Key header
    ANTHROPIC_API_KEY     optional; turns on the dashboard helper and suggested fixes (calls the Claude API)
    AUDITOR_ASSISTANT_MODEL  model id for the helper (default claude-sonnet-5-5)
    AUDITOR_AUTO_CREATE_TABLES  "true" (default) creates missing tables at startup, fine for SQLite and local use;
                          set "false" when the schema is managed by Alembic (Docker Compose does this)
    """
    return Settings(
        database_url=os.environ.get("AUDITOR_DATABASE_URL", "sqlite:///./agent_auditor.db"),
        api_key=os.environ.get("AUDITOR_API_KEY") or None,
        auto_create_tables=_flag(os.environ.get("AUDITOR_AUTO_CREATE_TABLES"), True),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        assistant_model=os.environ.get("AUDITOR_ASSISTANT_MODEL") or "claude-sonnet-5-5",
    )
