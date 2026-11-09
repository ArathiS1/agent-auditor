"""One command that checks every part of Agent Auditor against your RUNNING server and prints PASS or FAIL for each.

    python validation/selfcheck.py

It creates a throwaway agent called selfcheck-<number>, sends it data whose faults are planted on purpose (so the right
answer is known), then asks each module what it found. Nothing touches your real agents. The server must be running;
the address and key come from AUDITOR_URL / AUDITOR_API_KEY, or from the project's .env file.

Result words
  PASS  the module did what it should on data with a known answer
  FAIL  it did not; the line under it shows what the server actually said (paste that when asking for help)
  WARN  worth knowing, not a failure (for example the server accepts requests without a key)
  SKIP  could not be tried here
A PASS means "correct on this planted data", not "correct on every possible agent".
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "chatbot"))
try:
    from envfile import load_env

    load_env(ROOT / "chatbot")
except Exception:  # noqa: BLE001 - the .env file is a convenience only
    pass

BASE = os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000").rstrip("/")
KEY = os.environ.get("AUDITOR_API_KEY", "")
AGENT = f"selfcheck-{int(time.time())}"
NOW = datetime.now(timezone.utc)
HEAD = {"X-API-Key": KEY} if KEY else {}
client = httpx.Client(base_url=BASE, headers=HEAD, timeout=60)

rows: list[dict[str, Any]] = []
state: dict[str, Any] = {}


def api(method: str, path: str, **kw: Any) -> httpx.Response:
    return client.request(method, path, **kw)


def body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text[:300]}


def short(r: httpx.Response) -> str:
    return f"HTTP {r.status_code}: {json.dumps(body(r))[:400]}"


def check(group: str, name: str, fn: Callable[[], tuple[str, str]]) -> None:
    try:
        status, detail = fn()
    except Exception as exc:  # noqa: BLE001 - a crash is a result, not a reason to stop
        status, detail = "FAIL", f"{type(exc).__name__}: {exc}"[:400]
    rows.append({"group": group, "name": name, "status": status, "detail": detail})
    mark = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "SKIP": "SKIP"}[status]
    print(f"  [{mark}] {name}" + (f"\n         {detail}" if status != "PASS" and detail else ""), flush=True)


def ok(detail: str = "") -> tuple[str, str]:
    return "PASS", detail


def bad(detail: str) -> tuple[str, str]:
    return "FAIL", detail


# --------------------------------------------------------------------------- data with planted faults
def make_run(version: str, env: str, question: str, kind: str = "ok", age_h: float = 0.5, latency: int = 250) -> dict[str, Any]:
    started = NOW - timedelta(hours=age_h)
    docs = [{"source": f"kb{n}.md", "text": "article"} for n in range(3)]
    retrieval = {"kind": "retrieval", "name": "kb_search", "output": docs, "attributes": {"num_docs": 3, "scores": [0.9]}}
    run: dict[str, Any] = {
        "agent": AGENT, "version": version, "environment": env, "status": "ok", "input": {"question": question},
        "output": "Here is the answer from the article.",
        "started_at": started.isoformat(), "ended_at": (started + timedelta(milliseconds=latency)).isoformat(),
        "steps": [retrieval, {"kind": "llm", "name": "answer"}], "tags": {},
    }
    if kind == "tool_timeout":
        err = "TimeoutError: payments api timed out"
        run.update(status="error", error=err, output=None)
        run["steps"] = [retrieval, {"kind": "tool", "name": "payments_api", "status": "error", "error": err}, {"kind": "llm", "name": "answer"}]
    elif kind == "no_docs":
        run["steps"] = [{"kind": "retrieval", "name": "kb_search", "output": [], "attributes": {"num_docs": 0, "scores": []}}, {"kind": "llm", "name": "answer"}]
        run["output"] = "I could not find that."
    return run


FAIL_QUESTIONS = [
    ("tool_timeout", "What is the status of my payment for invoice 4471?"),
    ("tool_timeout", "Can you refund the charge on my card from last Tuesday?"),
    ("tool_timeout", "Why was my subscription billed twice this month?"),
    ("no_docs", "How do I configure the quantum flux capacitor?"),
    ("no_docs", "What is the airspeed of an unladen swallow?"),
    ("no_docs", "Explain the warranty on the moon base module."),
]
V1_CONFIG = {"model": "demo-1", "top_k": 3, "min_score": 0.3, "timeout": 5, "max_retries": 1}


def seed() -> tuple[str, str]:
    for label, prompt, config, activate in (
        ("v1", "You answer from the knowledge base.", V1_CONFIG, True),
        ("v2", "You answer from the knowledge base. Be very brief.", V1_CONFIG, False),  # only the prompt differs
        ("v3", "You answer from the knowledge base.", {**V1_CONFIG, "top_k": 4}, False),  # only a retrieval setting differs
    ):
        r = api("POST", f"/v1/agents/{AGENT}/versions", json={"label": label, "prompt": prompt, "config": config, "activate": activate})
        if r.status_code not in (200, 201):
            return "FAIL", f"could not register {label}: {short(r)}"
    runs: list[dict[str, Any]] = []
    for i in range(60):  # older, healthy, fast: the drift reference window
        runs.append(make_run("v1", "production", f"reference question number {i}", age_h=30 + i * 2, latency=250 + (i % 7) * 20))
    for n, (kind, q) in enumerate(FAIL_QUESTIONS):
        runs.append(make_run("v1", "production", q, kind, age_h=40 + n * 5, latency=300))
    for i in range(40):  # recent, healthy, SLOW: should show as drift, and is the canary baseline
        runs.append(make_run("v1", "production", f"current question number {i}", age_h=1 + i * 0.5, latency=1200 + (i % 7) * 20))
    for i in range(40):  # v2 canary: a third of its runs fail
        runs.append(make_run("v2", "canary", f"canary two question {i}", "tool_timeout" if i < 14 else "ok", age_h=0.5 + i * 0.3, latency=1250))
    for i in range(40):  # v3 canary: healthy and as fast as the baseline
        runs.append(make_run("v3", "canary", f"canary three question {i}", age_h=0.5 + i * 0.3, latency=1220 + (i % 7) * 20))
    bad_posts = []
    for run in runs:
        r = api("POST", "/v1/runs", json=run)
        if r.status_code != 201:
            bad_posts.append(short(r))
    if bad_posts:
        return "FAIL", f"{len(bad_posts)} of {len(runs)} runs were refused; first: {bad_posts[0]}"
    state["n_runs"] = len(runs)
    return "PASS", f"{len(runs)} runs sent for agent {AGENT}"


# --------------------------------------------------------------------------- the checks
def active_label() -> str | None:
    r = api("GET", f"/v1/agents/{AGENT}/versions")
    return next((v["label"] for v in body(r) if v.get("is_active")), None) if r.status_code == 200 else None


def main() -> int:
    print(f"\nAgent Auditor self-check against {BASE}\nThrowaway agent: {AGENT}\n")

    print("Basics")
    def t_health() -> tuple[str, str]:
        r = api("GET", "/v1/health")
        return ok() if r.status_code == 200 else bad(short(r))
    check("Basics", "Server is running and healthy", t_health)

    def t_auth() -> tuple[str, str]:
        r = httpx.get(f"{BASE}/v1/agents", headers={"X-API-Key": "definitely-wrong"}, timeout=20)
        if r.status_code == 401:
            return ok("requests with a wrong key are refused")
        return "WARN", "the server accepted a wrong key, so it runs without a key (fine on your own PC, not on a shared server)"
    check("Basics", "API key is enforced", t_auth)

    def t_dashboard() -> tuple[str, str]:
        r = httpx.get(f"{BASE}/dashboard", timeout=20)
        return ok() if r.status_code == 200 and "Agent Auditor" in r.text else bad(f"HTTP {r.status_code}")
    check("Basics", "Dashboard page is served", t_dashboard)

    print("\nSending planted data")
    check("Data", "Versions registered and runs accepted", seed)
    if not state.get("n_runs"):
        print("\nCannot continue without data. Fix the line above first.")
        return finish()

    def t_versions() -> tuple[str, str]:
        v = body(api("GET", f"/v1/agents/{AGENT}/versions"))
        return ok() if len(v) == 3 and active_label() == "v1" else bad(f"versions: {[(x['label'], x.get('is_active')) for x in v]}")
    check("Data", "Exactly one version (v1) is active", t_versions)

    def t_list_runs() -> tuple[str, str]:
        r = api("GET", "/v1/runs", params={"agent": AGENT, "limit": 200})
        return ok(f"{len(body(r))} newest runs listed") if r.status_code == 200 and len(body(r)) >= 100 else bad(short(r))
    check("Data", "Runs can be listed", t_list_runs)

    print("\nEvals")
    def t_evals() -> tuple[str, str]:
        written = 0
        for _ in range(6):
            r = api("POST", "/v1/evals/run", json={"agent": AGENT, "limit": 500})
            if r.status_code != 200:
                return bad(short(r))
            d = body(r)
            written += d["results_written"]
            if d["scorer_error_count"]:
                return bad(f"scorer errors: {d['scorer_errors']}")
            if not d["has_more"]:
                break
        s = body(api("GET", f"/v1/agents/{AGENT}/eval-summary"))
        labels = {v["version"]: v for v in s.get("versions", [])}
        if written and "v1" in labels and labels["v1"]["evaluated_runs"] > 0:
            return ok(f"{written} results written; v1 has {labels['v1']['evaluated_runs']} scored runs")
        return bad(f"written={written}, summary={json.dumps(s)[:300]}")
    check("Evals", "Runs are scored and summarised per version", t_evals)

    print("\nRoot causes")
    def t_diag() -> tuple[str, str]:
        for _ in range(6):
            r = api("POST", f"/v1/agents/{AGENT}/diagnose", json={"limit": 500})
            if r.status_code != 200:
                return bad(short(r))
            if not body(r)["has_more"]:
                break
        r = api("POST", f"/v1/agents/{AGENT}/root-causes", json={"diagnose_first": False})
        if r.status_code != 200:
            return bad(short(r))
        rep = body(r)
        causes = {c["cause"]: c["count"] for c in rep.get("causes", [])}
        state["causes"] = causes
        if "tool_timeout" in causes and ("retrieval_empty" in causes):
            return ok(f"found {causes}")
        return bad(f"expected tool_timeout and retrieval_empty among the causes, got {causes}")
    check("Root causes", "Planted failures are classified (tool timeout, empty retrieval)", t_diag)

    def t_taxonomy() -> tuple[str, str]:
        r = api("GET", "/v1/root-causes/taxonomy")
        return ok(f"{len(body(r))} causes defined") if r.status_code == 200 and len(body(r)) >= 8 else bad(short(r))
    check("Root causes", "Cause taxonomy is available", t_taxonomy)

    def t_sources() -> tuple[str, str]:
        r = api("POST", f"/v1/agents/{AGENT}/root-causes", json={"diagnose_first": False})
        return ok("suspect_sources is part of the report") if r.status_code == 200 and "suspect_sources" in body(r) else bad(short(r))
    check("Root causes", "Suspect-document analysis is part of the report", t_sources)

    print("\nFailure to test")
    def t_gen() -> tuple[str, str]:
        r = api("POST", f"/v1/agents/{AGENT}/tests/generate", json={})
        if r.status_code != 200:
            return bad(short(r))
        made = len(body(r)["created"])
        listed = body(api("GET", f"/v1/agents/{AGENT}/tests"))
        state["cases"] = len(listed)
        return ok(f"{made} regression tests created from failed runs") if made >= 1 and listed else bad(f"created={made}, listed={len(listed)}: {json.dumps(body(r))[:300]}")
    check("Failure to test", "Failed runs become regression tests", t_gen)

    print("\nDrift")
    def t_drift() -> tuple[str, str]:
        r = api("POST", "/v1/drift/check", json={"agent": AGENT, "current_hours": 24, "reference_hours": 168, "min_samples": 30})
        if r.status_code != 200:
            return bad(short(r))
        d = body(r)
        sig = [s["name"] for s in d.get("signals", []) if s.get("significant")]
        if d["overall"] in ("drift", "minor_drift"):
            return ok(f"overall={d['overall']}; significant signals: {sig}")
        return bad(f"latency rose from ~250 ms to ~1200 ms but overall={d['overall']} (reference_runs={d.get('reference_runs')}, current_runs={d.get('current_runs')})")
    check("Drift", "A planted slowdown is detected", t_drift)

    print("\nCanary and change attribution")
    def canary(cand: str) -> httpx.Response:
        return api("POST", f"/v1/agents/{AGENT}/canary/evaluate", json={"baseline": "v1", "candidate": cand})

    def t_canary_bad() -> tuple[str, str]:
        r = canary("v2")
        if r.status_code != 200:
            return bad(short(r))
        rep = body(r)
        state["v2"] = rep
        if rep["verdict"] in ("rollback", "hold"):
            return ok(f"verdict={rep['verdict']}; blocking={rep['blocking']}")
        return bad(f"v2 fails a third of its runs but verdict={rep['verdict']}: {rep['reasons']}")
    check("Canary", "A clearly worse candidate is NOT promoted", t_canary_bad)

    def t_canary_good() -> tuple[str, str]:
        r = canary("v3")
        if r.status_code != 200:
            return bad(short(r))
        rep = body(r)
        state["v3"] = rep
        if rep["verdict"] == "promote":
            return ok("a healthy candidate is promoted")
        return bad(f"v3 is healthy but verdict={rep['verdict']}: {rep['reasons']} (warnings: {rep.get('warnings')})")
    check("Canary", "A healthy candidate is promoted (no false alarm)", t_canary_good)

    def t_change() -> tuple[str, str]:
        rep = state.get("v2")
        if not rep:
            return "SKIP", "the canary check did not run"
        ch = next((c for c in rep["checks"] if c["name"] == "change"), None)
        if ch is None:
            return bad("the report has no 'change' check")
        att = (ch["details"].get("attribution") or [{}])[0].get("cause")
        return ok(f"attributed to {att}") if att == "prompt_regression" else bad(f"v2 differs from v1 only in its prompt, but attribution={att}; {ch['reason']}")
    check("Canary", "A regression is attributed to the prompt change", t_change)

    print("\nRelease gate")
    def release(cand: str, who: str = "alice") -> httpx.Response:
        return api("POST", f"/v1/agents/{AGENT}/releases", json={"candidate": cand, "proposed_by": who, "policy": {}})

    def t_block_bad() -> tuple[str, str]:
        r = release("v2")
        blocked, why = False, ""
        if r.status_code >= 400:
            blocked, why = True, f"refused at proposal ({r.status_code})"
        else:
            rid = body(r)["id"]
            a = api("POST", f"/v1/releases/{rid}/approve", json={"approver": "bob", "decision": "approve"})
            p = api("POST", f"/v1/releases/{rid}/promote", json={"actor": "bob"})
            blocked = p.status_code >= 400
            why = f"approve={a.status_code}, promote={p.status_code}"
            api("POST", f"/v1/releases/{rid}/cancel", json={"actor": "bob", "reason": "selfcheck cleanup"})
        if blocked and active_label() == "v1":
            return ok(f"the bad candidate could not be released ({why})")
        return bad(f"blocked={blocked} ({why}); active version is {active_label()}")
    check("Release gate", "A candidate that failed its canary cannot be released", t_block_bad)

    def t_self_approval() -> tuple[str, str]:
        r = release("v3")
        if r.status_code >= 400:
            return bad(f"could not propose v3: {short(r)}")
        state["rid"] = body(r)["id"]
        a = api("POST", f"/v1/releases/{state['rid']}/approve", json={"approver": "alice", "decision": "approve"})
        return ok(f"self-approval refused ({a.status_code})") if a.status_code >= 400 else bad("the proposer was able to approve their own release")
    check("Release gate", "Nobody can approve their own release", t_self_approval)

    def t_release_flow() -> tuple[str, str]:
        rid = state.get("rid")
        if not rid:
            return "SKIP", "no open release"
        a = api("POST", f"/v1/releases/{rid}/approve", json={"approver": "bob", "decision": "approve"})
        if a.status_code != 200:
            return bad(f"approve: {short(a)}")
        p = api("POST", f"/v1/releases/{rid}/promote", json={"actor": "bob"})
        if p.status_code != 200:
            return bad(f"promote: {short(p)}")
        if active_label() != "v3":
            return bad(f"promoted, but the active version is {active_label()}")
        rb = api("POST", f"/v1/releases/{rid}/rollback", json={"actor": "bob", "reason": "selfcheck: testing rollback"})
        if rb.status_code != 200:
            return bad(f"rollback: {short(rb)}")
        return ok("approve -> promote (v3 active) -> rollback (v1 active again)") if active_label() == "v1" else bad(f"after rollback the active version is {active_label()}")
    check("Release gate", "A good candidate: approve, promote, then roll back", t_release_flow)

    def t_audit() -> tuple[str, str]:
        r = api("GET", "/v1/audit")
        if r.status_code != 200:
            return bad(short(r))
        events = body(r)
        return ok(f"{len(events)} audit events recorded") if events else bad("the audit trail is empty")
    check("Release gate", "Every action is in the audit trail", t_audit)

    print("\nFix proposals")
    def t_fix() -> tuple[str, str]:
        r = api("POST", f"/v1/agents/{AGENT}/fixes/propose", json={})
        if r.status_code != 200:
            return bad(short(r))
        d = body(r)
        verifiable = [c for c in d["candidates"] if c.get("verifiable")]
        titles = [c["title"] for c in d["candidates"]][:4]
        if d["failed_runs"] >= 6 and verifiable:
            return ok(f"{len(d['candidates'])} candidates, e.g. {titles}")
        return bad(f"failed_runs={d['failed_runs']}, candidates={titles}, notes={d.get('notes')}")
    check("Fix proposals", "Failures produce testable fix candidates", t_fix)

    print("\nSDK with a real agent: trace, replay, and verify a fix")
    sdk_checks()
    return finish()


def sdk_checks() -> None:
    try:
        from agent_auditor.sdk import Auditor
    except Exception as exc:  # noqa: BLE001
        problem = f"{type(exc).__name__}: {exc}"
        check("SDK", "SDK can be imported", lambda: bad(problem))
        return
    aud = Auditor(BASE, agent=AGENT, api_key=KEY or None)

    def make_build() -> Callable[..., Callable]:
        def build(prompt: str | None = None, config: dict[str, Any] | None = None) -> Callable:
            top_k = int((config or {}).get("top_k", 3))

            @aud.observe("kb_search", kind="retrieval")
            def search(question: str) -> list[dict[str, str]]:
                # A toy agent with one real fault: with top_k=3 it finds nothing; from 4 up it finds documents.
                return [{"source": f"kb{n}.md", "text": "article"} for n in range(max(0, top_k - 3))]

            def agent(question: str) -> str:
                return "Here is the answer from the article." if search(question) else "I could not find that."
            return agent
        return build

    def t_trace() -> tuple[str, str]:
        with aud.run(input={"question": "sdk trace check"}, tags={"selfcheck": "1"}) as run:
            with run.step("kb_search", kind="retrieval", input="sdk trace check") as s:
                s.output = [{"source": "kb0.md", "text": "article"}]
            run.output = "Here is the answer from the article."
        aud.flush(20)
        r = api("GET", "/v1/runs", params={"agent": AGENT, "limit": 20})
        found = any(x.get("tags", {}).get("selfcheck") == "1" for x in body(r)) if r.status_code == 200 else False
        return ok("a run sent with the SDK arrived with its step") if found else bad("the run did not arrive; " + (short(r) if r.status_code != 200 else "not in the newest 20 runs"))
    check("SDK", "SDK sends a traced run to the server", t_trace)

    def t_replay() -> tuple[str, str]:
        rep = aud.replay_tests(make_build()(config={"top_k": 3}), version="v1", label="selfcheck baseline")
        s = rep.get("summary", {})
        if rep.get("client_errors"):
            return bad(f"agent errors: {rep['client_errors']}")
        return ok(f"verdict={rep['verdict']}; {s.get('passed')} pass, {s.get('failed')} fail of {s.get('total')}") if s.get("total") else bad(json.dumps(rep)[:300])
    check("SDK", "Regression tests replay against the agent and are graded", t_replay)

    def t_verify() -> tuple[str, str]:
        res = aud.verify_fixes(make_build(), max_candidates=4, guard=10)
        by = {r["candidate"]["title"]: r["status"] for r in res["results"]}
        top_k = next((r for r in res["results"] if r["candidate"]["changes"] and r["candidate"]["changes"][0]["field"] == "top_k"), None)
        others = [r for r in res["results"] if r is not top_k]
        if top_k is None:
            return bad(f"no top_k candidate was proposed: {by}")
        if top_k["status"] != "recommended":
            return bad(f"the working fix (top_k 3 -> 5) was not recommended: status={top_k['status']}, {top_k.get('judgement', {}).get('reason') or top_k.get('error')}")
        wrongly = [r["candidate"]["title"] for r in others if r["status"] == "recommended"]
        if wrongly:
            return bad(f"a change that does nothing was recommended: {wrongly}")
        return ok(f"only the real fix was recommended; others: { {k: v for k, v in by.items() if 'top_k' not in k} }")
    check("SDK", "A real fix is verified and irrelevant changes are rejected", t_verify)
    aud.close()


def finish() -> int:
    n = {k: sum(1 for r in rows if r["status"] == k) for k in ("PASS", "FAIL", "WARN", "SKIP")}
    print(f"\n{'=' * 64}\n{n['PASS']} passed, {n['FAIL']} failed, {n['WARN']} warnings, {n['SKIP']} skipped\n{'=' * 64}")
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    for g, items in groups.items():
        good = sum(1 for i in items if i["status"] == "PASS")
        print(f"  {g:<18} {good}/{len(items)}")
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"selfcheck-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"agent": AGENT, "server": BASE, "counts": n, "checks": rows}, indent=2), encoding="utf-8")
    print(f"\nFull results saved to {out}")
    if n["FAIL"]:
        print("Copy the FAIL lines above (with the text under them) into the chat and I will fix them.")
    return 1 if n["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
