"""Background worker: score and diagnose new runs so requests never have to.

It polls the database (no queue to run). Each cycle, for every agent and for the production and canary
environments, it evaluates runs that have no eval results yet and then diagnoses runs that have no diagnosis,
newest first, up to `batch` runs each. A failure on one agent is logged and the cycle carries on.

    agent-auditor-worker                 # loop forever, every 30 s
    agent-auditor-worker --once          # one cycle, then exit (cron, tests)

Running several workers is safe but wasteful: two workers can pick the same run, and the loser of that race
logs a skipped batch and retries next cycle. One worker per database is the intended setup.
"""
from __future__ import annotations

import argparse
import logging
import signal
import threading
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings
from .db import make_engine, make_session_factory
from .evals.service import evaluate_runs
from .models import Agent
from .rootcause.service import diagnose_runs

log = logging.getLogger("agent_auditor.worker")
ENVIRONMENTS = ("production", "canary")


def _process(db: Session, agent: str, environment: str, batch: int) -> dict[str, int]:
    scored = evaluate_runs(db, agent=agent, environment=environment, limit=batch, only_unevaluated=True)
    diagnosed = diagnose_runs(db, agent=agent, environment=environment, limit=batch)
    return {"evaluated": scored.evaluated_runs, "diagnosed": diagnosed["classified"]}


def run_once(session_factory: sessionmaker, *, batch: int = 200, environments: tuple[str, ...] = ENVIRONMENTS) -> dict[str, Any]:
    """One pass over every agent. Returns totals and the number of (agent, environment) batches that failed."""
    totals: dict[str, Any] = {"evaluated": 0, "diagnosed": 0, "errors": 0}
    try:
        with session_factory() as db:
            agents = list(db.scalars(select(Agent.name).order_by(Agent.name)))
    except Exception:  # noqa: BLE001 - for example the schema is not migrated yet; try again next cycle
        log.exception("worker: could not list agents")
        totals["errors"] += 1
        return totals
    for name in agents:
        for environment in environments:
            with session_factory() as db:
                try:
                    done = _process(db, name, environment, batch)
                    db.commit()
                except Exception:  # noqa: BLE001 - one bad batch must not stop the worker
                    db.rollback()
                    totals["errors"] += 1
                    log.exception("worker: batch failed for agent=%s environment=%s", name, environment)
                    continue
            totals["evaluated"] += done["evaluated"]
            totals["diagnosed"] += done["diagnosed"]
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-auditor-worker", description=__doc__.split("\n\n")[0])
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between cycles (default 30)")
    parser.add_argument("--batch", type=int, default=200, help="runs per agent, environment and step per cycle (max 500)")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    args = parser.parse_args(argv)
    if args.interval <= 0 or not 1 <= args.batch <= 500:
        parser.error("--interval must be positive and --batch between 1 and 500")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    factory = make_session_factory(make_engine(settings.database_url))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    while not stop.is_set():
        totals = run_once(factory, batch=args.batch)
        if totals["evaluated"] or totals["diagnosed"] or totals["errors"]:
            log.info("worker cycle: %s", totals)
        if args.once:
            return 1 if totals["errors"] else 0
        stop.wait(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
