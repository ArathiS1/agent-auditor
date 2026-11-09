#!/bin/sh
# api    : apply migrations, then serve (the default)
# worker : score and diagnose new runs in the background
# migrate: apply migrations and exit
set -eu
case "${1:-api}" in
  api)
    alembic upgrade head
    exec uvicorn agent_auditor.api.main:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    shift
    exec agent-auditor-worker "$@"
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  *)
    exec "$@"
    ;;
esac
