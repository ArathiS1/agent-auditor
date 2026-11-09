"""verify_fixes orchestration, with a stand-in Auditor (no server)."""
from agent_auditor.sdk.fixes import run_verification, similar

BASE = {"label": "v1", "prompt": "Be helpful.", "config": {"top_k": 3}}


def candidate(cid, **kw):
    c = {"id": cid, "kind": "config", "verifiable": True, "prompt": None, "config": {"top_k": 5}, "cause": "retrieval_empty", "title": cid}
    c.update(kw)
    return c


class FakeAuditor:
    def __init__(self, outcomes):
        self.outcomes, self.registered, self.guard_calls = outcomes, [], []

    def register_version(self, label, prompt, config, *, activate, use, agent):
        assert activate is False and use is False  # candidates must never become active
        self.registered.append((label, prompt, config))

    def replay_tests(self, fn, *, version, agent, label, call=None):
        outs = self.outcomes["base" if version == "v1" else version.split("-")[1]]
        return {"id": "r-" + version, "results": [{"case_id": f"c{i}", "outcome": o} for i, o in enumerate(outs)], "summary": {"total": len(outs)}}

    def _call_in_test(self, fn, inp, *, version, agent, call, tags):
        self.guard_calls.append((version, inp, tags))
        return fn(inp)

    def flush(self, timeout):
        return True


def run(outcomes, cands, guard_runs=(), build=None, **kw):
    aud = FakeAuditor(outcomes)
    proposal = {"base_version": BASE, "candidates": cands, "failed_runs": 5, "notes": []}
    build = build or (lambda prompt, config: (lambda q: "answer " + q))
    out = run_verification(aud, build, agent="bot", proposal=proposal, guard_runs=list(guard_runs), guard_same=None, call=None,
                           min_fixed_share=0.5, **kw)
    return aud, out


def test_only_candidates_that_pass_are_recommended_and_ranked():
    aud, out = run({"base": ["fail"] * 4, "good": ["pass"] * 4, "half": ["pass", "pass", "fail", "fail"], "bad": ["fail"] * 4},
                   [candidate("bad"), candidate("half"), candidate("good")])
    assert [x.split("-")[1] for x in out["recommended"]] == ["good", "half"]  # fully fixed first, then partly fixed
    assert out["results"][0]["status"] == "rejected"


def test_manual_candidates_are_listed_but_never_replayed():
    aud, out = run({"base": ["fail"], "good": ["pass"]}, [candidate("good"), {"id": "m", "kind": "manual", "verifiable": False, "title": "fix the bug"}])
    assert [m["id"] for m in out["manual"]] == ["m"] and len(aud.registered) == 1


def test_a_fix_that_changes_healthy_answers_is_flagged_risky():
    guard = [{"input": f"q{i}", "output": "totally different words here"} for i in range(5)]
    aud, out = run({"base": ["fail"] * 2, "good": ["pass"] * 2}, [candidate("good")], guard_runs=guard)
    [r] = out["results"]
    assert r["status"] == "risky" and out["recommended"] == []
    assert all(tags == {"guard": True} for _, _, tags in aud.guard_calls)


def test_a_fix_that_keeps_healthy_answers_is_recommended():
    guard = [{"input": "q", "output": "answer q"}] * 5
    _, out = run({"base": ["fail"] * 2, "good": ["pass"] * 2}, [candidate("good")], guard_runs=guard)
    assert out["results"][0]["status"] == "recommended" and out["results"][0]["guard"]["status"] == "ok"


def test_a_crashing_candidate_does_not_stop_the_others():
    def build(prompt, config):
        if config["top_k"] == 9:
            raise RuntimeError("cannot build")
        return lambda q: "answer " + q
    _, out = run({"base": ["fail"], "good": ["pass"]}, [candidate("boom", config={"top_k": 9}), candidate("good")], build=build)
    assert {r["status"] for r in out["results"]} == {"error", "recommended"}


def test_nothing_recommended_says_so():
    _, out = run({"base": ["fail"], "bad": ["fail"]}, [candidate("bad")])
    assert out["recommended"] == [] and "No candidate passed" in out["next_step"]


def test_label_includes_the_base_version_so_a_changed_base_cannot_reuse_it():
    aud, _ = run({"base": ["fail"], "good": ["pass"]}, [candidate("good")])
    assert aud.registered[0][0].startswith("fix-good-") and len(aud.registered[0][0].split("-")[2]) == 6


def test_similar_is_a_rough_word_overlap():
    assert similar("The refund window is 30 days", "refund window is 30 days") and not similar("yes", "completely unrelated reply")


def test_an_irrelevant_change_gets_no_credit_for_cases_that_already_pass():
    # 4 cases: the current version fails 2 and passes 2. The candidate changes nothing relevant, so its outcomes are identical.
    aud, out = run({"base": ["fail", "fail", "pass", "pass"], "idle": ["fail", "fail", "pass", "pass"]}, [candidate("idle")])
    assert out["results"][0]["status"] == "rejected" and out["recommended"] == []
    assert out["results"][0]["judgement"]["fixed"] == []


def test_a_fix_that_breaks_something_that_passed_is_rejected():
    _, out = run({"base": ["fail", "pass"], "risky": ["pass", "fail"]}, [candidate("risky")])
    assert out["results"][0]["status"] == "rejected" and "breaks 1" in out["results"][0]["judgement"]["reason"]
