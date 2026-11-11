"""The `agent-auditor` command.

    agent-auditor demo     start with sample data and open the dashboard (nothing is kept)
    agent-auditor serve    run the API and dashboard with your own database
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

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
