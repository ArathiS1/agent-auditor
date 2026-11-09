import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from agent_auditor.api.app import create_app
from agent_auditor.db import Base


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch) -> None:
    """Tests must not depend on keys set in your own terminal (an AUDITOR_API_KEY there made every request 401; a CHAT_PROVIDER in .env switched the chat tests to Gemini)."""
    for name in list(os.environ):
        if name in ("AUDITOR_API_KEY", "AUDITOR_URL", "AUDITOR_AGENT", "ANTHROPIC_API_KEY") or name.startswith(
                ("CHAT_", "GEMINI_", "GROQ_", "OPENROUTER_")):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    """An app on an in-memory SQLite database. Set TEST_DATABASE_URL (for example a throwaway PostgreSQL
    database) to run the same tests against another backend; its tables are dropped around every test."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        yield TestClient(create_app(database_url="sqlite://"))
        return
    app = create_app(database_url=url)
    Base.metadata.drop_all(app.state.engine)
    Base.metadata.create_all(app.state.engine)
    try:
        yield TestClient(app)
    finally:
        Base.metadata.drop_all(app.state.engine)
        app.state.engine.dispose()


@pytest.fixture(autouse=True)
def _fresh_router():
    """The chat's speed memory is process-wide; every test starts with a clean slate."""
    import sys
    r = sys.modules.get("router")
    if r is not None:
        r.reset()
    yield


@pytest.fixture(autouse=True)
def _no_real_provider_keys(monkeypatch):
    """Tests must not see the developer's real keys (from the shell or a .env), or "no key set" cases behave differently."""
    import os
    for name in list(os.environ):
        if name.startswith("AUDITOR_"):
            continue
        if name.endswith(("_API_KEY", "_BASE_URL", "_MODEL", "_TOOLS")) or name in ("CHAT_PROVIDER", "CHAT_AUTOROUTE", "CHAT_FOLLOWUPS"):
            monkeypatch.delenv(name, raising=False)
    yield
