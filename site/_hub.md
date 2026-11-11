# Documentation

Agent Auditor is an alpha open-source reliability layer for AI agents. This page gets you running; the pages on the left go deeper.

## Quick start

You need Python 3.11 or newer. The core needs no API key.

```
git clone https://github.com/ArathiS1/agent-auditor
cd agent-auditor
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
agent-auditor demo
```

`agent-auditor demo` starts the dashboard with made-up sample data (a support bot with a bad afternoon, a good candidate and a bad one) and opens it in your browser. Nothing is saved. You can also [click through a hosted copy](../demo/).

To run it for real with your own database, use `agent-auditor serve`.

On Windows, `powershell -ExecutionPolicy Bypass -File .\run-all.ps1` starts the Auditor and the chat app together. Add `-Check` to run a 23-point self-check.

## Use it with your own agent

```python
from agent_auditor.sdk import Auditor

auditor = Auditor("http://127.0.0.1:8000", agent="support-bot")
auditor.register_version("v1", prompt=PROMPT, config={"top_k": 4})

@auditor.observe(kind="retrieval")
def kb_search(q): ...

@auditor.observe_run()
def answer(question): ...
```

Plain Python and LangGraph/LangChain are supported. See `examples/quickstart_plain.py` and `examples/langgraph_demo.py` in the repository.

## The dashboard

Open `/dashboard`. Fifteen pages, grouped by what you are trying to do.

| Group | Pages |
|---|---|
| Watch | Overview, Failures, Runs |
| Understand | Quality, Root causes, Drift |
| Improve | Versions, Regression tests, Fixes, Benchmark, Gap analysis |
| Ship safely | Canary, Releases |
| Govern | Audit log, Setup |

## Where to read next

| Page | What is in it |
|---|---|
| [Reference](reference.html) | Every phase in detail: evals, drift, failure-to-test, root causes, canary, releases, attribution |
| [Architecture](architecture.html) | How the pieces fit together |
| [Deployment](deployment.html) | Docker, PostgreSQL and secrets |
| [Case study](case-study.html) | Agent Auditor watching its own chat agent, including what did not work |

## Known limitations

- Alpha software. It has run on one machine and is not proven under production load.
- Built-in scorers detect symptoms (errors, latency, empty answers), not whether an answer is factually right.
- Approver names are self-asserted behind one shared API key. Promotion marks a version active; it deploys nothing.
- Replay needs an agent that can be called again with the stored inputs.
- The AI helper can be wrong, so decisions stay with a person.
- The PostgreSQL CI job is experimental; SQLite is the tested backend.
