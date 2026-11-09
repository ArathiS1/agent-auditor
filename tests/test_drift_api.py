"""End-to-end tests for the drift endpoints (in-memory SQLite). Needs the full dev install."""
from datetime import timedelta

from drift_helpers import AS_OF, DRIFTED, window_payloads
from fastapi.testclient import TestClient

REF_START = AS_OF - timedelta(hours=24 + 168)
CUR_START = AS_OF - timedelta(hours=24)  # also the end of the reference window


def post_all(client: TestClient, payloads) -> None:
    for p in payloads:
        r = client.post("/v1/runs", json=p)
        assert r.status_code == 201, r.text


def seed(client: TestClient, agent="bot", ref_n=90, cur_n=40, ref_kw=None, cur_kw=None) -> None:
    for label in ("v1", "v2"):
        r = client.post(f"/v1/agents/{agent}/versions", json={"label": label, "prompt": label, "activate": False})
        assert r.status_code in (200, 201), r.text
    post_all(client, window_payloads(agent, REF_START, CUR_START, ref_n, **(ref_kw or {})))
    post_all(client, window_payloads(agent, CUR_START, AS_OF, cur_n, **(cur_kw or {})))


def check(client: TestClient, **overrides):
    body = {"agent": "bot", "as_of": AS_OF.isoformat(), "include_evals": False, **overrides}
    return client.post("/v1/drift/check", json=body)


def signals(report: dict) -> dict:
    return {s["name"]: s for s in report["signals"]}


def test_no_change_is_stable_and_windows_are_reported(client: TestClient):
    seed(client)
    r = check(client)
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["overall"] == "stable", [(s["name"], s["severity"], s["reason"]) for s in report["signals"] if s["severity"] != "none"]
    assert report["reference_runs"] == 90 and report["current_runs"] == 40
    assert report["windows"]["current_end"].startswith("2026-09-30T12:00:00")
    assert report["windows"]["reference_end"] == report["windows"]["current_start"]


def test_real_drift_is_detected_with_explanations(client: TestClient):
    seed(client, cur_kw=DRIFTED)
    report = check(client).json()
    assert report["overall"] == "drift"
    s = signals(report)
    assert s["latency_ms"]["severity"] == "high" and s["latency_ms"]["effect_name"] == "ks_statistic"
    assert s["empty_retrieval"]["severity"] == "high" and s["empty_retrieval"]["current"]["rate"] > 0.3
    assert s["input_terms"]["severity"] in ("medium", "high")
    assert "refund" in [t["term"] for t in s["input_terms"]["details"]["more_common"]]
    assert s["tool_calls"]["severity"] in ("medium", "high")
    ranks = {"high": 3, "medium": 2, "low": 1, "none": 0, "insufficient_data": -1}
    order = [ranks[x["severity"]] for x in report["signals"]]
    assert order == sorted(order, reverse=True)  # worst first


def test_too_few_runs_in_a_window_is_insufficient_data(client: TestClient):
    seed(client, cur_n=10)
    report = check(client).json()
    assert report["overall"] == "insufficient_data" and report["signals"] == []


def test_reports_are_saved_listed_and_retrievable(client: TestClient):
    seed(client, cur_kw=DRIFTED)
    saved = check(client).json()
    assert saved["id"] and saved["created_at"]
    again = client.get(f"/v1/drift/reports/{saved['id']}").json()
    assert again["overall"] == "drift" and len(again["signals"]) == len(saved["signals"])

    unsaved = check(client, save=False).json()
    assert unsaved["id"] is None

    listing = client.get("/v1/agents/bot/drift").json()
    assert [r["id"] for r in listing] == [saved["id"]]
    assert listing[0]["overall"] == "drift" and "latency_ms:high" in listing[0]["flagged"]


def test_eval_pass_rates_are_compared_when_runs_have_been_scored(client: TestClient):
    seed(client, cur_kw=DRIFTED)
    scored = client.post("/v1/evals/run", json={"agent": "bot", "limit": 500}).json()
    assert scored["evaluated_runs"] == 130
    with_evals = signals(check(client, include_evals=True).json())
    assert with_evals["eval:retrieval_nonempty"]["severity"] == "high"
    assert with_evals["eval:run_succeeded"]["severity"] == "none"
    assert "eval:retrieval_nonempty" not in signals(check(client, include_evals=False).json())


def test_version_mix_change_is_called_out_and_version_filter_works(client: TestClient):
    seed(client, ref_kw={"version": "v1"}, cur_kw={"version": "v2"})
    report = check(client).json()
    assert any("version mix differs" in n for n in report["notes"])
    filtered = check(client, version="v1").json()  # v1 has no runs in the current window
    assert filtered["overall"] == "insufficient_data" and filtered["current_runs"] == 0


def test_only_the_requested_environment_is_analysed(client: TestClient):
    seed(client)
    post_all(client, window_payloads("bot", CUR_START, AS_OF, 40, environment="canary", **DRIFTED))
    assert check(client).json()["overall"] == "stable"  # canary runs do not leak into production
    assert check(client, environment="canary").json()["overall"] == "insufficient_data"


def test_large_windows_are_capped_and_the_cap_is_reported(client: TestClient):
    seed(client)
    report = check(client, max_runs=50).json()
    assert report["reference_runs"] == 50 and report["reference_total_runs"] == 90
    assert any("analysed the newest 50" in n for n in report["notes"])


def test_validation_and_not_found(client: TestClient):
    seed(client)
    assert check(client, agent="ghost").status_code == 404
    assert check(client, version="zzz").status_code == 404
    assert check(client, current_hours=0).status_code == 422
    assert check(client, min_samples=1).status_code == 422
    assert check(client, alpha=1.5).status_code == 422
    assert check(client, max_runs=10).status_code == 422
    assert client.post("/v1/drift/check", json={}).status_code == 422
    assert client.get("/v1/drift/reports/missing").status_code == 404
    assert client.get("/v1/agents/ghost/drift").status_code == 404
