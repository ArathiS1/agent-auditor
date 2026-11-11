# Publishing to PyPI

This is for the maintainer. After it is done once, every GitHub release publishes itself.

## One-time setup

1. Make an account at https://pypi.org and turn on two-factor sign-in.
2. Check the name is free: open https://pypi.org/project/agent-auditor/. If the page exists, the name is taken; change
   `name` in `pyproject.toml` (for example `agent-auditor-ai`) and the install line in the README.
3. On PyPI go to **Your account, Publishing, Add a new pending publisher** and fill in:
   - PyPI project name: `agent-auditor`
   - Owner: `ArathiS1`
   - Repository name: `agent-auditor`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
4. On GitHub go to the repository **Settings, Environments, New environment**, name it `pypi`, and save.

## Each release

1. Change `version` in `pyproject.toml`, `src/agent_auditor/__init__.py` and `src/agent_auditor/api/app.py` to the same number.
2. Commit and push.
3. On GitHub: **Releases, Draft a new release**, create a tag such as `v0.13.0`, and click **Publish release**.
4. Open the **Actions** tab. The "Publish to PyPI" run builds the package, checks it and uploads it.
5. Test it in a clean folder:
   ```
   python -m venv try && try\Scripts\activate
   pip install agent-auditor
   agent-auditor demo
   ```

## Check a build without publishing

```
pip install build twine
python -m build
twine check dist/*
```
