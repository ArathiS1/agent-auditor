"""Record a read-only copy of the dashboard into site/demo/, for GitHub Pages.

Developer tool (not shipped to users). It starts `agent-auditor demo`, opens every dashboard page in a
browser, saves the data each page asked for, and writes:
    site/demo/index.html     the dashboard page, with demo.js loaded first
    site/demo/snapshot.json  the recorded answers
    site/demo/demo.js        answers the dashboard's data calls from the snapshot (no server needed)

Run from the repository root:   pip install playwright && python site/build_demo.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site" / "demo"
PORT = 8140
BASE = f"http://127.0.0.1:{PORT}"
AGENT = "support-bot"
VIEWS = ["overview", "failures", "runs", "versions", "quality", "causes", "drift", "tests", "fixes",
         "canary", "releases", "benchmark", "gap", "audit", "setup"]
TIME_PARAMS = {"since", "until", "from", "to", "as_of"}


def key_for(url: str) -> str:
    """Same rule as demo.js: the path plus the query, without moving time stamps, sorted."""
    u = urlparse(url)
    q = sorted((k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k not in TIME_PARAMS)
    return u.path + ("?" + urlencode(q) if q else "")


def main() -> None:
    proc = subprocess.Popen([sys.executable, "-m", "agent_auditor", "demo", "--no-browser", "--port", str(PORT)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            if "/dashboard" in line:
                break
        responses: dict[str, object] = {}

        def grab(resp) -> None:
            if resp.request.method == "GET" and urlparse(resp.url).path.startswith("/v1/") and resp.status == 200:
                try:
                    responses[key_for(resp.url)] = resp.json()
                except Exception:  # noqa: BLE001 - not JSON, skip
                    pass

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("response", grab)
            page.goto(f"{BASE}/dashboard")
            page.evaluate(f"localStorage.setItem('aa.agent', '{AGENT}')")
            for win in ("7d", "24h", "1h"):
                page.evaluate(f"localStorage.setItem('aa.win', '{win}')")
                for view in VIEWS:
                    page.goto(f"{BASE}/dashboard#/{view}")
                    page.reload()
                    page.wait_for_timeout(700)
            browser.close()

        # Detail pages the dashboard opens on click: runs, canary and drift reports, releases.
        with httpx.Client(base_url=BASE, timeout=30) as http:
            def add(path: str) -> object:
                r = http.get(path)
                if r.status_code == 200:
                    responses[key_for(path)] = r.json()
                    return r.json()
                return None

            runs = http.get("/v1/runs", params={"agent": AGENT, "limit": 200}).json()
            failed = [r for r in runs if r["status"] != "ok"]
            for r in failed[:60] + [r for r in runs if r["status"] == "ok"][:25]:
                add(f"/v1/runs/{r['id']}")
            for c in http.get(f"/v1/agents/{AGENT}/canary").json():
                add(f"/v1/canary/{c['id']}")
            for d in http.get(f"/v1/agents/{AGENT}/drift").json():
                add(f"/v1/drift/reports/{d['id']}")
            for rel in http.get(f"/v1/agents/{AGENT}/releases").json():
                add(f"/v1/releases/{rel['id']}")
                add(f"/v1/releases/{rel['id']}/audit")
    finally:
        proc.terminate()

    # The page comes from the package, with the one-time security nonce removed (GitHub Pages sets no CSP).
    OUT.mkdir(parents=True, exist_ok=True)
    src = _fetch_source()
    src = re.sub(r' nonce="[^"]*"', "", src).replace("<head>", '<head>\n<script src="demo.js"></script>', 1)
    (OUT / "index.html").write_text(src, encoding="utf-8")
    by_path: dict[str, object] = {}
    for k, v in responses.items():
        by_path.setdefault(k.split("?")[0], v)
    snap = {"generated_at": datetime.now(timezone.utc).isoformat(), "responses": responses, "byPath": by_path}
    (OUT / "snapshot.json").write_text(json.dumps(snap, separators=(",", ":")), encoding="utf-8")
    print(f"recorded {len(responses)} responses, {(OUT / 'snapshot.json').stat().st_size // 1024} KB")


def _fetch_source() -> str:
    """The dashboard's HTML straight from the package."""
    from agent_auditor.api.app import _dashboard_page

    return _dashboard_page().replace("__NONCE__", "")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"done in {time.time() - t0:.0f}s")
