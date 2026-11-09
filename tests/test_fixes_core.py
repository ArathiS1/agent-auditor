"""Fix proposal rules and the replay judge (pure logic, no server)."""
from agent_auditor.fixes import judge_guard, judge_replay, propose
from agent_auditor.fixes.propose import GROUNDING_RULE


def cause(name, count=10, share=0.5, subcauses=()):
    return {"cause": name, "count": count, "share_of_failures": share, "advice": ["check it"],
            "subcauses": [{"name": s, "count": 1} for s in subcauses]}


def test_weak_retrieval_proposes_one_setting_at_a_time():
    out = propose("p", {"top_k": 4, "min_score": 0.35}, {"causes": [cause("retrieval_low_relevance")]})
    fields = [c["changes"][0]["field"] for c in out]
    assert fields == ["top_k", "min_score"] and all(c["verifiable"] for c in out)
    assert out[0]["config"]["top_k"] == 6 and out[0]["config"]["min_score"] == 0.35  # only one thing changed
    assert out[1]["config"]["min_score"] == 0.25 and out[1]["config"]["top_k"] == 4


def test_missing_settings_become_a_manual_item_not_an_invented_change():
    [c] = propose("p", {}, {"causes": [cause("retrieval_empty")]})
    assert c["kind"] == "manual" and not c["verifiable"] and c["config"] is None


def test_ungrounded_answers_get_a_prompt_rule_and_lower_temperature():
    out = propose("Be helpful.", {"temperature": 0.7}, {"causes": [cause("ungrounded_answer")]})
    prompt_c = next(c for c in out if c["kind"] == "prompt")
    assert prompt_c["prompt"].startswith("Be helpful.") and GROUNDING_RULE in prompt_c["prompt"]
    assert any(c["changes"][0]["field"] == "temperature" and c["config"]["temperature"] == 0 for c in out)


def test_rule_already_in_the_prompt_is_not_proposed_again():
    out = propose(GROUNDING_RULE, {}, {"causes": [cause("ungrounded_answer")]})
    assert all(c["kind"] != "prompt" for c in out)


def test_timeouts_and_rate_limits():
    out = propose("p", {"timeout": 5, "max_retries": 1}, {"causes": [cause("tool_timeout")]})
    assert {c["changes"][0]["field"]: c["changes"][0]["to"] for c in out} == {"timeout": 10, "max_retries": 2}
    rl = propose("p", {"max_retries": 2}, {"causes": [cause("llm_error", subcauses=["rate_limit"])]})
    assert rl[0]["config"]["max_retries"] == 3


def test_code_level_causes_and_suspect_documents_are_manual_only():
    report = {"causes": [cause("tool_error"), cause("agent_exception")],
              "suspect_sources": [{"source": "old.md", "failed_share": 0.8, "healthy_share": 0.05}]}
    out = propose("p", {"top_k": 3}, report)
    assert out and all(c["kind"] == "manual" for c in out)
    assert any("old.md" in c["title"] for c in out)


def test_verifiable_candidates_come_first_and_ids_are_stable():
    report = {"causes": [cause("tool_error"), cause("retrieval_empty")]}
    a = propose("p", {"top_k": 3}, report)
    b = propose("p", {"top_k": 3}, report)
    assert [c["id"] for c in a] == [c["id"] for c in b]
    assert a[0]["verifiable"] and not a[-1]["verifiable"]


def _replay(*outcomes):
    return {"results": [{"case_id": f"c{i}", "outcome": o} for i, o in enumerate(outcomes)], "summary": {"total": len(outcomes)}}


def test_judge_replay_levels():
    assert judge_replay(_replay("pass", "pass"))["status"] == "verified"
    assert judge_replay(_replay("pass", "pass", "fail"))["status"] == "improved"
    r = judge_replay(_replay("pass", "fail", "fail"))
    assert r["status"] == "rejected" and r["still_failing"] == ["c1", "c2"]
    assert judge_replay(_replay("pass", "missing"))["status"] == "incomplete"
    assert judge_replay({"results": []})["status"] == "incomplete"


def test_judge_guard():
    ok = [{"same": True}] * 9 + [{"same": False}]
    assert judge_guard(ok)["status"] == "ok"
    bad = [{"same": False}] * 5 + [{"same": True}] * 5
    assert judge_guard(bad)["status"] == "risky"
    assert judge_guard([{"same": True}, {"same": True, "error": "boom"}])["status"] == "risky"
    assert judge_guard([])["status"] == "not_run"


def test_judge_with_a_baseline_only_credits_what_the_change_fixed():
    base = _replay("fail", "fail", "pass", "pass")
    same = judge_replay(_replay("fail", "fail", "pass", "pass"), baseline=base)
    assert same["status"] == "rejected" and same["fixed"] == []
    assert judge_replay(_replay("pass", "pass", "pass", "pass"), baseline=base)["status"] == "verified"
    assert judge_replay(_replay("pass", "fail", "pass", "pass"), baseline=base)["status"] == "improved"
    broke = judge_replay(_replay("pass", "pass", "fail", "pass"), baseline=base)
    assert broke["status"] == "rejected" and broke["regressed"] == ["c2"]
    assert judge_replay(_replay("pass", "pass"), baseline=_replay("pass", "pass"))["status"] == "rejected"
