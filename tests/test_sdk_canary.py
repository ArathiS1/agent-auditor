"""SDK side of Phase 7: the canary router and the evaluate_canary client call."""
import asyncio

from agent_auditor.sdk import Auditor, AuditorError


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p, self.text = status, payload if payload is not None else {}, str(payload)

    def json(self):
        return self._p


class Http:
    def __init__(self, status=200):
        self.calls, self.runs, self.status = [], [], status

    def get(self, path, params=None, headers=None):
        self.calls.append(("GET", path, params))
        return Resp(self.status, [{"id": "c1"}])

    def post(self, path, json=None, headers=None):
        if path == "/v1/runs":
            self.runs.append(json)
            return Resp(201, {})
        self.calls.append(("POST", path, json))
        return Resp(self.status, {"verdict": "continue"})

    def close(self):
        pass


def make(status=200):
    http = Http(status)
    return Auditor(agent="bot", http_client=http, background=False, api_key="k"), http


def v1(question):
    return f"v1:{question}"


def v2(question):
    return f"v2:{question}"


def test_same_key_always_gets_same_arm():
    auditor, _ = make()
    router = auditor.canary_router(baseline=("v1", v1), candidate=("v2", v2), fraction=0.3)
    first = {k: router.choose(k) for k in range(200)}
    assert first == {k: router.choose(k) for k in range(200)}


def test_fraction_is_roughly_respected_and_edges_are_exact():
    auditor, _ = make()
    router = auditor.canary_router(baseline=("v1", v1), candidate=("v2", v2), fraction=0.2)
    share = sum(router.choose(f"user-{i}") == "candidate" for i in range(4000)) / 4000
    assert 0.17 < share < 0.23
    router.set_fraction(0.0)
    assert all(router.choose(i) == "baseline" for i in range(50))
    router.set_fraction(1.0)
    assert all(router.choose(i) == "candidate" for i in range(50))


def test_salt_changes_assignment():
    auditor, _ = make()
    a = auditor.canary_router(baseline=("v1", v1), candidate=("v2", v2), fraction=0.5, salt="a")
    b = auditor.canary_router(baseline=("v1", v1), candidate=("v2", v2), fraction=0.5, salt="b")
    assert any(a.choose(i) != b.choose(i) for i in range(100))


def test_runs_are_recorded_with_arm_environment_and_version():
    auditor, http = make()
    router = auditor.canary_router(baseline=("v1", v1), candidate=("v2", v2), fraction=0.5)
    keys = list(range(40))
    for k in keys:
        expected = "v2" if router.choose(k) == "candidate" else "v1"
        assert router.call(k, "hi") == f"{expected}:hi"
    assert len(http.runs) == 40
    for k, run in zip(keys, http.runs):
        arm = router.choose(k)
        assert run["environment"] == ("canary" if arm == "candidate" else "production")
        assert run["version"] == ("v2" if arm == "candidate" else "v1")
        assert run["tags"]["canary_arm"] == arm
        assert run["input"] == {"question": "hi"}
    stats = router.stats()
    assert stats["baseline"] + stats["candidate"] == 40 and stats["candidate"] > 0 and stats["baseline"] > 0


def test_observe_run_functions_are_not_double_wrapped():
    auditor, http = make()

    @auditor.observe_run()
    def agent_v2(question):
        return "v2!"

    router = auditor.canary_router(baseline=("v1", v1), candidate=("v2", agent_v2), fraction=1.0)
    assert router.call("k", "q") == "v2!"
    assert len(http.runs) == 1 and http.runs[0]["environment"] == "canary" and http.runs[0]["version"] == "v2"


def test_candidate_error_is_recorded_and_reraised():
    auditor, http = make()

    def broken(question):
        raise RuntimeError("boom")

    router = auditor.canary_router(baseline=("v1", v1), candidate=("v2", broken), fraction=1.0)
    try:
        router.call("k", "q")
    except RuntimeError:
        pass
    else:
        raise AssertionError("the error must propagate")
    assert http.runs[0]["status"] == "error" and http.runs[0]["environment"] == "canary"


def test_async_arms():
    auditor, http = make()

    async def av2(question):
        return "async v2"

    router = auditor.canary_router(baseline=("v1", v1), candidate=("v2", av2), fraction=1.0)
    assert asyncio.run(router.acall("k", "q")) == "async v2"
    assert http.runs[0]["version"] == "v2"


def test_override_does_not_leak_after_the_call():
    auditor, http = make()
    router = auditor.canary_router(baseline=("v1", v1), candidate=("v2", v2), fraction=1.0)
    router.call("k", "q")
    with auditor.run(input="plain") as run:
        run.output = "x"
    assert http.runs[-1]["environment"] == "production" and http.runs[-1]["version"] != "v2"


def test_router_validation():
    auditor, _ = make()
    for kwargs in (
        dict(baseline=("v1", v1), candidate=("v1", v2)),
        dict(baseline=("v1", v1), candidate=("v2", v2), fraction=1.5),
        dict(baseline=v1, candidate=("v2", v2)),
    ):
        try:
            auditor.canary_router(**kwargs)
        except (ValueError, TypeError):
            continue
        raise AssertionError("expected an error")


def test_evaluate_canary_posts_policy_only_when_given():
    auditor, http = make()
    assert auditor.evaluate_canary(baseline="v1", candidate="v2")["verdict"] == "continue"
    method, path, body = http.calls[0]
    assert (method, path) == ("POST", "/v1/agents/bot/canary/evaluate")
    assert body["baseline"] == "v1" and body["candidate"] == "v2" and "policy" not in body
    auditor.evaluate_canary(baseline="v1", candidate="v2", policy={"min_canary_runs": 100})
    assert http.calls[1][2]["policy"] == {"min_canary_runs": 100}


def test_list_and_get_reports():
    auditor, http = make()
    assert auditor.list_canary_reports(candidate="v2", limit=5) == [{"id": "c1"}]
    assert http.calls[0] == ("GET", "/v1/agents/bot/canary", {"limit": 5, "candidate": "v2"})
    auditor.get_canary_report("abc")
    assert http.calls[1][1] == "/v1/canary/abc"


def test_errors_surface_as_auditor_error():
    auditor, _ = make(status=404)
    try:
        auditor.evaluate_canary(baseline="v1", candidate="v2")
    except AuditorError:
        return
    raise AssertionError("expected AuditorError")
