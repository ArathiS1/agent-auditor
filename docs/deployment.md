# Deployment

## Docker Compose (API + worker + PostgreSQL)

```bash
cp .env.example .env        # then set POSTGRES_PASSWORD and AUDITOR_API_KEY
docker compose up --build
curl http://127.0.0.1:8000/readyz        # {"status":"ok"}
```
Compose refuses to start without both variables. The API container applies migrations (`alembic upgrade head`) before
it serves; the worker starts after the API reports ready. Data lives in the `pgdata` volume.

Point the SDK at it:
```python
auditor = Auditor("http://127.0.0.1:8000", agent="support-bot", api_key="<AUDITOR_API_KEY>")
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AUDITOR_DATABASE_URL` | `sqlite:///./agent_auditor.db` | SQLAlchemy URL. Use `postgresql+psycopg://user:pass@host/db` in production. |
| `AUDITOR_API_KEY` | unset (open) | When set, every `/v1` request must send it as `X-API-Key`. `/healthz` and `/readyz` stay open. |
| `ANTHROPIC_API_KEY` | unset (helper off) | Optional. Turns on the dashboard's AI helper and "Suggest a fix". Questions send the dashboard's figures to the Claude API. |
| `AUDITOR_ASSISTANT_MODEL` | `claude-sonnet-5-5` | Model the helper uses. |
| `AUDITOR_AUTO_CREATE_TABLES` | `true` | Create missing tables at startup. Set `false` when Alembic manages the schema (Compose does). |

## Migrations

```bash
alembic upgrade head                       # new database
alembic stamp head                         # database that the app created itself (SQLite default)
alembic revision --autogenerate -m "..."   # after changing models/
```
Read the note at the top of `migrations/versions/0001_baseline.py` before changing a model for the first time.
Run migrations from one place at a time; two API replicas starting together can race on the first upgrade.

## Worker

`agent-auditor-worker` scores and diagnoses new runs for every agent every 30 seconds (production and canary
environments). `--once` runs a single cycle. Run one worker per database.

## Without Docker

```bash
pip install -e ".[postgres]"
export AUDITOR_DATABASE_URL=postgresql+psycopg://...
alembic upgrade head
AUDITOR_AUTO_CREATE_TABLES=false uvicorn agent_auditor.api.main:app --host 0.0.0.0
agent-auditor-worker
```

## Before you expose it

- Put it behind TLS (a reverse proxy or your platform's load balancer). The API key travels in a header.
- Treat the API key as a shared secret, not as user identity. Approver and actor names in releases are
  self-asserted strings; the audit trail is a labelled record, not proof of who acted.
- Back up the database. The audit trail and the saved reports live there.
- Ingested runs contain your agents' inputs and outputs. Decide what you are allowed to store, and redact in the SDK
  (`capture_input=False` on `observe_run`) before sending anything sensitive.
