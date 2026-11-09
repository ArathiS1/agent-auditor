"""Plan the traffic, evaluate it (offline or on a server), and turn the answers into honest numbers."""
from __future__ import annotations

import csv
import dataclasses
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import support_bot as A
import kb  # noqa: F401

# truth effect -> root-cause labels the tool could reasonably give. "stale" has none: the tool cannot see it.
ACCEPT = {
    "tool_timeout": {"tool_timeout"}, "llm_error": {"llm_error"}, "slow_run": {"slow_run"},
    "retrieval": {"retrieval_empty", "retrieval_low_relevance"}, "stale": set(),
}
CAUSE_TO_EFFECT = {c: e for e, cs in ACCEPT.items() for c in cs}


@dataclass(frozen=True)
class Scenario:
    name: str
    blurb: str
    cfg: dict
    expect: str  # the verdict a careful person would want
    visible: bool  # can the built-in checks see this fault at all?


SCENARIOS = [
    Scenario("control", "harmless change: the prompt is reworded", {"prompt": "Answer briefly and politely, using only the help center text."}, "promote", True),
    Scenario("timeout", "the policy API times out on 25% of runs", {"timeout_rate": 0.25}, "rollback", True),
    Scenario("rate_limit", "the model call is rate limited on 20% of runs", {"rate_limit_rate": 0.2}, "rollback", True),
    Scenario("retrieval", "half of the help-center articles are missing from the search index", {"index_drop": 0.5}, "rollback", True),
    Scenario("slow", "the policy API takes about 12 seconds on 30% of runs", {"slow_rate": 0.3}, "rollback", True),
    Scenario("stale", "40% of articles are out of date (right article, wrong fact)", {"stale_share": 0.4}, "rollback", False),
]


@dataclass
class Canary:
    scenario: Scenario
    rep: int
    label: str
    records: list
    config: dict
    result: dict | None = None


@dataclass
class Plan:
    agent: str
    baseline: list
    canaries: list
    baseline_config: dict


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def accuracy(records: list) -> float:
    return sum(r.truth.correct for r in records) / max(len(records), 1)


def make_plan(*, agent: str, baseline_runs: int, canary_runs: int, reps: int, control_reps: int, seed: int, llm=None) -> Plan:
    now = datetime.now(timezone.utc)
    base_bot = A.SupportBot(A.Config("v1", seed=seed), llm)
    baseline = base_bot.traffic(baseline_runs, random.Random(f"base|{seed}"), now - timedelta(hours=20), timedelta(hours=18), agent, "production")
    canaries = []
    for sc in SCENARIOS:
        for rep in range(1, (control_reps if sc.name == "control" else reps) + 1):
            label = f"{sc.name}-r{rep}"
            bot = A.SupportBot(A.Config(label, seed=seed * 1000 + rep, **sc.cfg), llm)
            recs = bot.traffic(canary_runs, random.Random(f"{label}|{seed}"), now - timedelta(hours=2), timedelta(hours=1.5), agent, "canary")
            canaries.append(Canary(sc, rep, label, recs, dataclasses.asdict(bot.cfg)))
    return Plan(agent, baseline, canaries, dataclasses.asdict(base_bot.cfg))


# ------------------------------------------------------------------ evaluation backends
def evaluate_offline(plan: Plan, progress=print) -> None:
    import offline
    base_views = [offline.to_view(r, i) for i, r in enumerate(plan.baseline)]
    for r, d in zip(plan.baseline, offline.diagnose(base_views)):
        r.diagnosis = d
    for c in plan.canaries:
        views = [offline.to_view(r, i) for i, r in enumerate(c.records)]
        c.result = offline.canary(base_views, views)
        for r, d in zip(c.records, offline.diagnose(views)):
            r.diagnosis = d
        progress(f"  {c.label:<14} -> {c.result['verdict']}")


def evaluate_server(plan: Plan, url: str, api_key: str | None, progress=print) -> None:
    import httpx
    headers = {"X-API-Key": api_key} if api_key else {}
    with httpx.Client(base_url=url, timeout=120, headers=headers) as http:
        http.get("/healthz").raise_for_status()
        # The server treats identical prompt + config as the same version, so each label carries its real settings
        # (which also differ by label and seed). That is also what you would record for a real version.
        versions = [("v1", plan.baseline_config)] + [(c.label, c.config) for c in plan.canaries]
        for label, config in versions:
            http.post(f"/v1/agents/{plan.agent}/versions",
                      json={"label": label, "prompt": config["prompt"], "config": config, "activate": False}).raise_for_status()

        def send(records):
            for i, r in enumerate(records):
                resp = http.post("/v1/runs", json=r.payload)
                resp.raise_for_status()
                r.run_id = resp.json()["id"]
                if (i + 1) % 100 == 0:
                    progress(f"    sent {i + 1}/{len(records)}")

        progress(f"  sending {len(plan.baseline)} baseline runs ...")
        send(plan.baseline)
        for c in plan.canaries:
            send(c.records)
            resp = http.post(f"/v1/agents/{plan.agent}/canary/evaluate", json={"baseline": "v1", "candidate": c.label})
            resp.raise_for_status()
            c.result = resp.json()
            progress(f"  {c.label:<14} -> {c.result['verdict']}")
        progress("  reading the tool's root-cause label for every run ...")
        every = plan.baseline + [r for c in plan.canaries for r in c.records]
        for i, r in enumerate(every):
            resp = http.get(f"/v1/runs/{r.run_id}/diagnosis")
            resp.raise_for_status()
            d = resp.json()
            r.diagnosis = {"status": d["status"], "cause": d["cause"], "subcause": d.get("subcause"), "step": d.get("step")}
            if (i + 1) % 500 == 0:
                progress(f"    read {i + 1}/{len(every)}")


# ------------------------------------------------------------------ metrics
def named_causes(result: dict) -> list[str]:
    """Causes the canary's 'causes' check said became more common, largest first."""
    check = next((c for c in result["checks"] if c["name"] == "causes"), None)
    rows = (check or {}).get("details", {}).get("causes", [])
    worse = sorted((r for r in rows if r.get("significant") and r.get("delta", 0) > 0), key=lambda r: -r["delta"])
    return [r["cause"] for r in worse]


def summarize(plan: Plan) -> dict:
    base_acc = accuracy(plan.baseline)
    scen = {}
    for sc in SCENARIOS:
        cs = [c for c in plan.canaries if c.scenario.name == sc.name]
        verdicts = {}
        for c in cs:
            verdicts[c.result["verdict"]] = verdicts.get(c.result["verdict"], 0) + 1
        n = len(cs)
        rolled = [c for c in cs if c.result["verdict"] == "rollback"]
        want = next(iter(ACCEPT.get(_effect_of(sc), set())), None)
        cause_ok = sum(1 for c in rolled if named_causes(c.result) and named_causes(c.result)[0] in ACCEPT.get(_effect_of(sc), set()))
        scen[sc.name] = {
            "blurb": sc.blurb, "expect": sc.expect, "visible": sc.visible, "reps": n, "verdicts": verdicts,
            "rollback": len(rolled), "flagged": sum(1 for c in cs if c.result["verdict"] in ("rollback", "hold")),
            "promote": verdicts.get("promote", 0), "cause_named_ok": cause_ok, "cause_expected": sorted(ACCEPT.get(_effect_of(sc), set())) or None,
            "accuracy": sum(accuracy(c.records) for c in cs) / max(n, 1), "want": want,
            "wilson_rollback": wilson(len(rolled), n),
        }
    labels = _label_table(plan)
    return {"baseline_runs": len(plan.baseline), "baseline_accuracy": base_acc,
            "baseline_silent_wrong": sum((not r.truth.correct) and r.payload["status"] == "ok" for r in plan.baseline),
            "scenarios": scen, "labels": labels}


def _effect_of(sc: Scenario) -> str | None:
    return {"timeout": "tool_timeout", "rate_limit": "llm_error", "retrieval": "retrieval", "slow": "slow_run", "stale": "stale"}.get(sc.name)


def _label_table(plan: Plan) -> dict:
    rows: dict = {}
    every = plan.baseline + [r for c in plan.canaries if c.scenario.name != "control" for r in c.records]
    for r in every:
        if r.diagnosis is None:
            continue
        eff = r.truth.effect or "none"
        row = rows.setdefault(eff, {"n": 0, "flagged": 0, "correct": 0, "labels": {}})
        row["n"] += 1
        cause = r.diagnosis["cause"] if r.diagnosis["status"] == "failed" else None
        if cause:
            row["flagged"] += 1
            row["labels"][cause] = row["labels"].get(cause, 0) + 1
        if r.truth.effect in ACCEPT and cause in ACCEPT[r.truth.effect]:
            row["correct"] += 1
    gt = [rows[e] for e in ACCEPT if e != "stale" and e in rows]
    n = sum(x["n"] for x in gt)
    k = sum(x["correct"] for x in gt)
    return {"by_effect": rows, "labelable_n": n, "labelable_correct": k, "wilson": wilson(k, n)}


def write_labeling_sheet(plan: Plan, path, n: int = 30, seed: int = 0) -> int:
    pool = [r for r in plan.baseline + [r for c in plan.canaries for r in c.records]
            if r.diagnosis and r.diagnosis["status"] == "failed"]
    random.Random(seed).shuffle(pool)
    pick = pool[:n]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["n", "question", "answer", "run_error", "what_the_steps_show", "tool_label", "your_label"])
        for i, r in enumerate(pick, 1):
            steps = "; ".join(
                f"{s['name']}: " + (f"ERROR {s['error']}" if s.get("status") == "error" else
                                    (f"score {s['attributes']['scores'][0]} ({s['attributes']['num_docs']} docs)" if s["kind"] == "retrieval" and s["attributes"].get("scores")
                                     else "no documents" if s["kind"] == "retrieval" else f"{_ms(s):.0f} ms"))
                for s in r.payload["steps"])
            w.writerow([i, r.payload["input"]["question"], r.payload["output"] or "", r.payload["error"] or "", steps,
                        r.diagnosis["cause"], ""])
    return len(pick)


def _ms(step: dict) -> float:
    return (datetime.fromisoformat(step["ended_at"]) - datetime.fromisoformat(step["started_at"])).total_seconds() * 1000


def score_manual(path) -> dict:
    """Compare your_label with tool_label from the filled-in sheet."""
    from agent_auditor.rootcause.classify import CAUSES
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    done = [r for r in rows if (r.get("your_label") or "").strip()]
    bad = sorted({r["your_label"].strip() for r in done} - set(CAUSES) - {"none"})
    agree = sum(r["your_label"].strip() == r["tool_label"] for r in done)
    wrong = [(r["n"], r["tool_label"], r["your_label"].strip()) for r in done if r["your_label"].strip() != r["tool_label"]]
    return {"labelled": len(done), "of": len(rows), "agree": agree, "unknown_labels": bad, "disagreements": wrong,
            "wilson": wilson(agree, len(done)) if done else None}


def timed(fn):
    t = time.time()
    out = fn()
    return out, time.time() - t
