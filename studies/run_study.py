"""Compare two models on a stock agent with Agent Auditor's canary, using the agent's own OpenTelemetry instrumentation.

  pip install "agent-auditor[otel]" opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
  pip install smolagents openinference-instrumentation-smolagents          # for --agent smolagents
  pip install langchain langchain-openai langgraph openinference-instrumentation-langchain   # for --agent langgraph

  python studies/run_study.py --fake                                      # dry run, no key, scripted model
  export OPENAI_API_KEY=...                                               # any OpenAI-compatible provider
  python studies/run_study.py --agent smolagents --baseline gpt-4o-mini --candidate gpt-4.1-nano --runs 30

Writes studies/results/<agent>-<baseline>-vs-<candidate>.{md,json}. Use --base-url and --api-key-env for another provider.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE)]

import study_agents as agents  # noqa: E402
import study_report as report  # noqa: E402
import taskset as tools  # noqa: E402


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-")


def _start_auditor(db: str, key: str, port: int):
    import httpx
    import uvicorn

    from agent_auditor.api.app import create_app

    server = uvicorn.Server(uvicorn.Config(create_app(database_url=db, api_key=key), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if httpx.get(url + "/healthz", timeout=2).status_code == 200:
                return server, url
        except httpx.HTTPError:
            time.sleep(0.2)
    raise SystemExit(f"The Auditor did not start on port {port}. Is the port in use? Try --port with another number.")


def _provider(url: str, key: str, service: str, version: str, environment: str):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    prov = TracerProvider(resource=Resource.create({"service.name": service, "service.version": version,
                                                    "deployment.environment.name": environment}))
    prov.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=url + "/v1/traces", headers={"x-api-key": key})))
    return prov


def run_phase(agent: str, model: str, label: str, environment: str, n: int, *, base_url: str, api_key: str,
              auditor_url: str, auditor_key: str, service: str, say=print) -> list[dict]:
    prov = _provider(auditor_url, auditor_key, service, label, environment)
    off = agents.instrument(agent, prov)
    rows: list[dict] = []
    try:
        ask = agents.build(agent, model, base_url, api_key)
        for i, (question, _tool, _args, expected) in enumerate(tools.sample(n), 1):
            t0, answer, error = time.perf_counter(), "", None
            try:
                answer = ask(question)
            except Exception as exc:  # noqa: BLE001 - an agent failing is a result, not a crash
                error = f"{type(exc).__name__}: {exc}"[:300]
            rows.append({"question": question, "expected": expected, "answer": answer, "error": error,
                         "correct": tools.is_correct(answer, expected) and not error, "seconds": round(time.perf_counter() - t0, 3)})
            if i % 10 == 0 or i == n:
                say(f"  {model} ({environment}): {i}/{n} done, {sum(r['correct'] for r in rows)} correct")
    finally:
        off()
        prov.force_flush()
        prov.shutdown()
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", choices=agents.AGENTS, default="smolagents")
    ap.add_argument("--baseline", default="gpt-4o-mini", help="model id of the version currently in production")
    ap.add_argument("--candidate", default="gpt-4.1-nano", help="model id of the version you are thinking of switching to")
    ap.add_argument("--runs", type=int, default=30, help="questions per side (the canary wants at least 30 by default)")
    ap.add_argument("--base-url", default="https://api.openai.com/v1", help="any OpenAI-compatible endpoint")
    ap.add_argument("--api-key-env", default="OPENAI_API_KEY", help="name of the environment variable that holds the provider key")
    ap.add_argument("--auditor-url", default="", help="an Auditor that is already running; by default one is started for the study")
    ap.add_argument("--auditor-key", default="study-key")
    ap.add_argument("--db", default=f"sqlite:///{HERE / 'study.db'}", help="database for the started Auditor (open it later with agent-auditor serve --db ...)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--scorers", default="", help="extra scorers for the canary, comma separated, e.g. answer_judge (needs the judge env vars)")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--fake", action="store_true", help="dry run against a scripted model; the candidate is treated as a weak one")
    args = ap.parse_args(argv)

    if args.fake:
        import fake_server
        fake_server.start(8774)
        base_url, api_key = "http://127.0.0.1:8774/v1", "fake"
        if "weak" not in args.candidate:
            args.candidate = "weak-" + args.candidate
        args.db = "sqlite:///" + str(Path(args.out) / "fake-study.db")
    else:
        api_key = os.environ.get(args.api_key_env, "")
        base_url = args.base_url
        if not api_key:
            print(f"No key found in ${args.api_key_env}. Set it, or try --fake first to check the setup without a key.")
            return 2
    if args.baseline == args.candidate:
        print("--baseline and --candidate are the same model; a comparison needs two.")
        return 2
    Path(args.out).mkdir(parents=True, exist_ok=True)
    if args.fake and (Path(args.out) / "fake-study.db").exists():
        (Path(args.out) / "fake-study.db").unlink()

    server = None
    auditor_url = args.auditor_url.rstrip("/")
    if not auditor_url:
        server, auditor_url = _start_auditor(args.db, args.auditor_key, args.port)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    service = f"study-{args.agent}"
    base_label, cand_label = f"{args.baseline}@{stamp}", f"{args.candidate}@{stamp}"
    print(f"{'DRY RUN (fake model) - ' if args.fake else ''}{args.agent}: {args.baseline} vs {args.candidate}, {args.runs} questions each")
    common = dict(base_url=base_url, api_key=api_key, auditor_url=auditor_url, auditor_key=args.auditor_key, service=service)
    base_rows = run_phase(args.agent, args.baseline, base_label, "production", args.runs, **common)
    cand_rows = run_phase(args.agent, args.candidate, cand_label, "canary", args.runs, **common)

    from agent_auditor.sdk import Auditor

    scorers = [s.strip() for s in args.scorers.split(",") if s.strip()] or None
    canary = Auditor(auditor_url, agent=service, api_key=args.auditor_key).evaluate_canary(
        baseline=base_label, candidate=cand_label, since_hours=6.0, scorers=scorers,
        policy={"min_canary_runs": args.runs, "min_baseline_runs": args.runs})
    study = {"agent": args.agent, "baseline": args.baseline, "candidate": args.candidate, "n": args.runs, "fake": args.fake,
             "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "baseline_rows": base_rows, "candidate_rows": cand_rows, "canary": canary}
    name = ("DRYRUN-" if args.fake else "") + _slug(f"{args.agent}-{args.baseline}-vs-{args.candidate}")
    (Path(args.out) / f"{name}.json").write_text(json.dumps(study, indent=2, default=str), encoding="utf-8")
    (Path(args.out) / f"{name}.md").write_text(report.build(study), encoding="utf-8")
    print(f"\nVERDICT for {args.candidate}: {canary['verdict'].upper()}")
    for r in canary["reasons"]:
        print("  -", r)
    print(f"\nReport: {Path(args.out) / (name + '.md')}")
    if server is not None and not args.fake:
        print(f"Browse the runs: agent-auditor serve --db {args.db} --api-key {args.auditor_key}  then open /dashboard")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
