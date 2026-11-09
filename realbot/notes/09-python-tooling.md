# Python tooling


Use virtual environments: python -m venv .venv keeps project packages separate. pytest finds tests in files named test_*.py.

Type hints and pydantic models validate data coming from APIs. Use httpx for HTTP calls and set an explicit timeout on every request.

ruff is a fast linter and formatter that replaces flake8 and black. Use asyncio when waiting on many network calls at once; it does not speed up CPU work.
