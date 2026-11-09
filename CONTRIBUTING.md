# Contributing

Bug reports and small fixes are welcome.

1. `python -m venv .venv`, activate it, then `pip install -e ".[dev]"`.
2. `ruff check src tests examples chatbot` and `pytest -q` must both pass before you open a pull request.
3. Keep changes small and add a test for what you change.
4. Never commit `.env`, `chat.db` or any API key. If you find a security problem, open an issue that says so without posting details, and the maintainer will reply with a private way to share them.

Good first issues: a new built-in scorer, a new root-cause rule, a new provider preset in `chatbot/providers.py`.
