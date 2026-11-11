"""The `agent-auditor` command.

    agent-auditor demo     start with sample data and open the dashboard (nothing is kept)
    agent-auditor serve    run the API and dashboard with your own database
    agent-auditor import-otlp FILE   send OpenTelemetry traces saved as JSON to a running Auditor
    agent-auditor --version
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import webbrowser

from . import __version__


def _free_port(host: str, wanted: int) -> int:
    for port in range(wanted, wanted + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                s.bind((host, port))
            except OSError:
                continue
            return port
    raise SystemExit(f"No free port found from {wanted} to {wanted + 29}. Use --port to pick one.")


def _wait_until_up(base: str, seconds: float = 30.0) -> None:
    import httpx

    end = time.time() + seconds
    while time.time() < end:
        try:
            if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise SystemExit("The server did not start in time.")


def cmd_demo(args: argparse.Namespace) -> int:
    import httpx
    import uvicorn

    tmp = tempfile.mkdtemp(prefix="agent-auditor-demo-")
    os.environ["AUDITOR_DATABASE_URL"] = "sqlite:///" + os.path.join(tmp, "demo.db").replace("\\", "/")
    os.environ.pop("AUDITOR_API_KEY", None)  # the demo is local and open
    os.environ["AUDITOR_AUTO_CREATE_TABLES"] = "true"
    from .api.main import app  # imported after the settings above, because the app reads them when it is built
    from .demo import seed

    port = _free_port(args.host, args.port)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://{args.host}:{port}"
    try:
        print("Agent Auditor demo: starting with sample data (made up; nothing is saved).")
        _wait_until_up(base)
        with httpx.Client(base_url=base, timeout=60) as http:
            seed(http)
        url = base + "/dashboard"
        print(f"\nOpen {url}\nPick the agent 'support-bot', then try Failures, Canary and Releases. Press Ctrl+C to stop.")
        if not args.no_browser:
            webbrowser.open(url)
        if args.exit_after_seed:  # used by the tests
            return 0
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    if args.db:
        os.environ["AUDITOR_DATABASE_URL"] = args.db
    if args.api_key:
        os.environ["AUDITOR_API_KEY"] = args.api_key
    print(f"Agent Auditor {__version__}: dashboard at http://{args.host}:{args.port}/dashboard")
    uvicorn.run("agent_auditor.api.main:app", host=args.host, port=args.port, log_level="info")
    return 0


def read_otlp_file(path: str) -> list[dict]:
    """OTLP/JSON requests from a file: one JSON object, or one object per line (what the OpenTelemetry Collector's file exporter writes)."""
    import json

    text = open(path, encoding="utf-8-sig").read()
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except ValueError:
        out = []
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError as exc:
                    raise SystemExit(f"{path}, line {n}: not valid JSON ({exc})") from exc
        return out


def import_otlp_file(path: str, client, *, agent: str | None = None, environment: str | None = None,
                     version: str | None = None) -> dict:
    """Send each OTLP request in the file to /v1/traces. `client` is an httpx.Client already pointed at the Auditor."""
    params = {k: v for k, v in (("agent", agent), ("environment", environment), ("version", version)) if v}
    total = {"requests": 0, "spans": 0, "runs_created": 0, "runs_updated": 0}
    agents: set[str] = set()
    for payload in read_otlp_file(path):
        r = client.post("/v1/traces", json=payload, params=params)
        if r.status_code != 200:
            detail = r.text[:300]
            raise SystemExit(f"The Auditor refused the traces ({r.status_code}): {detail}")
        done = r.json().get("imported", {})
        total["requests"] += 1
        for k in ("spans", "runs_created", "runs_updated"):
            total[k] += int(done.get(k, 0))
        agents.update(done.get("agents", []))
    return dict(total, agents=sorted(agents))


def cmd_import_otlp(args: argparse.Namespace) -> int:
    import httpx

    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    with httpx.Client(base_url=args.url.rstrip("/"), headers=headers, timeout=60) as client:
        try:
            done = import_otlp_file(args.file, client, agent=args.agent, environment=args.environment, version=args.version)
        except httpx.HTTPError as exc:
            print(f"Could not reach the Auditor at {args.url}: {exc}")
            return 1
    print(f"Imported {done['spans']} spans from {done['requests']} request(s): {done['runs_created']} new runs, "
          f"{done['runs_updated']} updated, agents: {', '.join(done['agents']) or 'none'}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-auditor", description="Reliability layer for AI agents.")
    parser.add_argument("--version", action="version", version=f"agent-auditor {__version__}")
    sub = parser.add_subparsers(dest="command")

    demo = sub.add_parser("demo", help="start with sample data and open the dashboard")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8000)
    demo.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    demo.add_argument("--exit-after-seed", action="store_true", help=argparse.SUPPRESS)
    demo.set_defaults(func=cmd_demo)

    serve = sub.add_parser("serve", help="run the API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", help="database URL (default: a local SQLite file, agent_auditor.db)")
    serve.add_argument("--api-key", help="require this key in the X-API-Key header")
    serve.set_defaults(func=cmd_serve)

    imp = sub.add_parser("import-otlp", help="send OpenTelemetry traces saved as JSON to a running Auditor")
    imp.add_argument("file", help="OTLP/JSON file: one request, or one request per line")
    imp.add_argument("--url", default=os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000"))
    imp.add_argument("--api-key", default=os.environ.get("AUDITOR_API_KEY", ""))
    imp.add_argument("--agent", help="agent name (default: the service.name in the traces)")
    imp.add_argument("--environment", choices=["production", "canary", "test"], help="default: from deployment.environment, else production")
    imp.add_argument("--version", help="version label (default: service.version in the traces)")
    imp.set_defaults(func=cmd_import_otlp)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
