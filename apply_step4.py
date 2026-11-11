"""One-time patch for step 4 (OpenTelemetry import). Run it once from the project folder: python apply_step4.py

It makes two small edits that a zip would overwrite along with your version number:
  - src/agent_auditor/api/app.py   registers the /v1/traces route
  - pyproject.toml                  adds the [otel] extra and the test dependencies
Safe to run twice. You can delete this file afterwards.
"""
from pathlib import Path

root = Path(__file__).resolve().parent


def patch(path: str, edits: list[tuple[str, str]]) -> None:
    file = root / path
    text = file.read_text(encoding="utf-8")
    changed = False
    for old, new in edits:
        if new in text:
            continue
        if old not in text:
            raise SystemExit(f"{path}: could not find the text to change:\n  {old!r}\nSend this message back and it will be fixed.")
        text = text.replace(old, new, 1)
        changed = True
    file.write_text(text, encoding="utf-8")
    print(("patched " if changed else "already done: ") + path)


patch("src/agent_auditor/api/app.py", [
    ("from .release_routes import router as release_router\n",
     "from .release_routes import router as release_router\nfrom .otlp_routes import router as otlp_router\n"),
    ("    app.include_router(router)\n", "    app.include_router(router)\n    app.include_router(otlp_router)\n"),
])
patch("pyproject.toml", [
    ('dev = ["pytest>=8", "ruff>=0.5"]',
     'dev = ["pytest>=8", "ruff>=0.5", "opentelemetry-proto>=1.20", "opentelemetry-sdk>=1.20", "opentelemetry-exporter-otlp-proto-http>=1.20"]'),
    ('langgraph = ["langgraph>=0.2", "langchain-core>=0.3"]',
     'langgraph = ["langgraph>=0.2", "langchain-core>=0.3"]\notel = ["opentelemetry-proto>=1.20"]'),
])
print("Done. Now run: pip install -e \".[dev]\"   and   python -m pytest -q")
