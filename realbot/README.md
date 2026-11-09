# A real bot to test Agent Auditor

A small question-answering bot over your own notes (RAG): a **search** step finds passages, an **answer** step
writes the reply. Every question is sent to Agent Auditor as a trace, so you can watch failures appear on the dashboard.

Files: `bot.py` (the bot), `run_questions.py` (sends the questions), `notes/` (12 sample notes, replace with yours),
`questions.csv` (168 questions: 144 answerable, 24 off-topic; half are typos/short forms).

Run every command from the project folder (`G:\agent-auditor`) with the venv active.

1. Start the server (terminal 1):  `$env:AUDITOR_API_KEY="<your key>"` then `uvicorn agent_auditor.api.app:create_app --factory`
2. (Optional) Claude answers instead of a plain extract (terminal 2):  `$env:ANTHROPIC_API_KEY="sk-ant-..."`  and add `$env:AUDITOR_API_KEY="<same key>"`.
   Without it, or with `--no-llm`, the bot returns the best matching sentence. Costs pennies with Claude (about 170 short calls).
3. Try the bot alone:  `python realbot\bot.py "What is hybrid search?"`
4. Baseline (v1, production):  `python realbot\run_questions.py --repeat 2`
5. Dashboard:  http://127.0.0.1:8000/dashboard , agent `notes-bot`.
6. Healthy new version (expect promote):  `python realbot\run_questions.py --version v2 --env canary --evaluate`
7. Bad release (expect rollback):  `python realbot\run_questions.py --version v3 --env canary --break search --evaluate`
   and a rate-limit outage:  `python realbot\run_questions.py --version v4 --env canary --break llm --evaluate`

`--break` simulates a bad release or outage in 35% of runs. It is for testing the auditor, not part of a real bot.

## Use your own notes
Put `.md`, `.txt` or `.pdf` files in `realbot/notes/` (PDFs need `pip install pypdf`). Separate ideas with a blank line.
Then edit `questions.csv` (columns `question,expected,in_scope`; `expected` is a word the right answer must contain).
