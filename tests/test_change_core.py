"""Change attribution and suspect-source detection (pure logic, no server)."""
from agent_auditor.evals.types import RunView, StepView
from agent_auditor.rootcause.change import attribute_change, diff_versions
from agent_auditor.rootcause.sources import run_sources, suspect_sources


def test_diff_groups_changes_by_component():
    d = diff_versions("Be brief.", {"model": "a", "top_k": 4, "timeout": 5, "style": "x"},
                      "Be brief and kind.", {"model": "b", "top_k": 4, "timeout": 5, "style": "x"})
    assert d["changed"] == ["prompt", "model"]
    assert d["details"]["model"] == [{"key": "model", "from": "a", "to": "b"}]


def test_identical_versions_have_no_changes():
    assert diff_versions("p", {"top_k": 3}, "p", {"top_k": 3})["changed"] == []


def test_single_prompt_change_with_fitting_cause_is_strong():
    d = diff_versions("old", {}, "new", {})
    [a] = attribute_change(d, ["ungrounded_answer"], regressed=True)
    assert a["cause"] == "prompt_regression" and a["confidence"] == "strong"


def test_single_model_change_is_named_model_change():
    d = diff_versions("p", {"model": "small"}, "p", {"model": "large"})
    assert attribute_change(d, ["llm_error"], regressed=True)[0]["cause"] == "model_change"


def test_several_changes_are_not_blamed_on_one_thing():
    d = diff_versions("old", {"model": "a"}, "new", {"model": "b"})
    [a] = attribute_change(d, ["ungrounded_answer"], regressed=True)
    assert a["cause"] == "multiple_changes" and a["confidence"] == "weak"


def test_retrieval_getting_worse_with_no_retrieval_change_points_at_the_data():
    d = diff_versions("old", {}, "new", {})
    causes = [a["cause"] for a in attribute_change(d, ["retrieval_empty"], regressed=True)]
    assert "source_data_change" in causes and "prompt_regression" in causes


def test_identical_versions_that_regress_point_at_the_data_or_traffic():
    d = diff_versions("p", {"top_k": 3}, "p", {"top_k": 3})
    assert attribute_change(d, [], regressed=True)[0]["cause"] == "source_data_change"


def test_no_regression_means_no_attribution():
    assert attribute_change(diff_versions("a", {}, "b", {}), ["ungrounded_answer"], regressed=False) == []


def _run(i, docs):
    step = StepView(kind="retrieval", name="search", output=[{"source": d, "text": "x"} for d in docs])
    return RunView(id=str(i), steps=(step,))


def test_run_sources_reads_names_from_retrieved_items():
    assert run_sources(_run(1, ["a.md", "b.md"])) == {"a.md", "b.md"}
    assert run_sources(RunView(id="x", steps=(StepView(kind="retrieval", name="s", output=["plain text"]),))) == set()


def test_a_document_concentrated_in_failures_is_flagged():
    failed = [_run(i, ["old_policy.md", "faq.md"]) for i in range(12)]
    healthy = [_run(100 + i, ["faq.md", "guide.md"]) for i in range(40)]
    found = suspect_sources(failed, healthy)
    assert [f["source"] for f in found] == ["old_policy.md"]
    assert found[0]["failed_share"] == 1.0 and found[0]["healthy_share"] == 0.0


def test_a_document_everyone_retrieves_is_not_flagged():
    failed = [_run(i, ["faq.md"]) for i in range(12)]
    healthy = [_run(100 + i, ["faq.md"]) for i in range(40)]
    assert suspect_sources(failed, healthy) == []


def test_too_few_runs_flags_nothing():
    assert suspect_sources([_run(1, ["a"])], [_run(2, ["b"])]) == []
