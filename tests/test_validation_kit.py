"""The validation kit's own logic (pure Python: no server, no database)."""
import csv
import random
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "validation"))

import support_bot as A  # noqa: E402
import harness as H  # noqa: E402
import kb  # noqa: E402

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def test_question_set_is_the_expected_size_and_every_fact_has_four_questions():
    qs = kb.questions()
    assert len(kb.FACTS) == 30 and len(qs) == 130
    assert all(len(f[4]) == 4 for f in kb.FACTS)
    assert all(f[1].count("{v}") == 1 and f[2] != f[3] for f in kb.FACTS)


def test_healthy_retrieval_finds_the_right_article_most_of_the_time():
    idx = kb.Index(kb.passages())
    inscope = [q for q in kb.questions() if q.fact_id]
    hits = sum(1 for q in inscope if (r := idx.search(q.text, 1)) and r[0][0].id == q.fact_id)
    assert hits / len(inscope) >= 0.85


def test_runs_are_repeatable_for_the_same_seed():
    def go():
        bot = A.SupportBot(A.Config("v1", timeout_rate=0.3))
        return [r.payload["output"] for r in bot.traffic(40, random.Random(5), NOW, A.timedelta(hours=1), "a", "canary")]
    assert go() == go()


def test_timeout_fault_makes_error_runs_with_the_tool_timeout_truth():
    bot = A.SupportBot(A.Config("v2", timeout_rate=1.0))
    rec = bot.traffic(5, random.Random(1), NOW, A.timedelta(hours=1), "a", "canary")[0]
    assert rec.payload["status"] == "error" and rec.truth.effect == "tool_timeout" and not rec.truth.correct
    assert any(s["name"] == "policy_api" and s["status"] == "error" for s in rec.payload["steps"])


def test_stale_facts_give_confident_wrong_answers_the_checks_cannot_see():
    bot = A.SupportBot(A.Config("v2", stale_share=1.0))
    recs = [r for r in bot.traffic(60, random.Random(2), NOW, A.timedelta(hours=1), "a", "canary") if r.truth.in_scope]
    wrong = [r for r in recs if not r.truth.correct]
    assert wrong and all(r.payload["status"] == "ok" and r.truth.effect == "stale" for r in wrong if r.truth.effect)


def test_payload_matches_the_api_schema():
    from agent_auditor.schemas import RunIn
    rec = A.SupportBot(A.Config("v1")).traffic(3, random.Random(3), NOW, A.timedelta(hours=1), "a", "production")[0]
    run = RunIn(**rec.payload)
    assert run.environment == "production" and run.steps and run.steps[0].kind == "retrieval"


def test_wilson_interval_is_sane():
    lo, hi = H.wilson(5, 5)
    assert 0.5 < lo < 1.0 and hi == 1.0
    lo, hi = H.wilson(0, 10)
    assert lo == 0.0 and 0.2 < hi < 0.35


def test_offline_plan_runs_end_to_end_and_summarizes():
    plan = H.make_plan(agent="t", baseline_runs=60, canary_runs=40, reps=1, control_reps=1, seed=2)
    H.evaluate_offline(plan, progress=lambda s: None)
    s = H.summarize(plan)
    assert set(s["scenarios"]) == {sc.name for sc in H.SCENARIOS}
    assert s["scenarios"]["timeout"]["verdicts"]  # a verdict was produced
    assert s["labels"]["by_effect"]["tool_timeout"]["correct"] == s["labels"]["by_effect"]["tool_timeout"]["flagged"]


def test_labeling_sheet_round_trip():
    plan = H.make_plan(agent="t", baseline_runs=60, canary_runs=40, reps=1, control_reps=1, seed=3)
    H.evaluate_offline(plan, progress=lambda s: None)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sheet.csv"
        n = H.write_labeling_sheet(plan, path, n=10)
        rows = list(csv.DictReader(open(path, encoding="utf-8")))
        assert n == len(rows) <= 10
        for r in rows:
            r["your_label"] = r["tool_label"]
        rows[0]["your_label"] = "not_a_label"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        res = H.score_manual(path)
        assert res["labelled"] == len(rows) and res["agree"] == len(rows) - 1 and res["unknown_labels"] == ["not_a_label"]


def test_every_version_has_distinct_content_so_the_server_keeps_them_apart():
    import hashlib
    import json
    plan = H.make_plan(agent="t", baseline_runs=10, canary_runs=10, reps=2, control_reps=3, seed=4)
    def digest(cfg):
        return hashlib.sha256(json.dumps([cfg["prompt"], cfg], sort_keys=True).encode()).hexdigest()
    seen = [digest(plan.baseline_config)] + [digest(c.config) for c in plan.canaries]
    assert len(set(seen)) == len(seen)
    assert all(c.config["label"] == c.label for c in plan.canaries)
