"""SDK side of replay: calling the agent with stored inputs, tagging runs, talking to the server.
Uses a fake HTTP client, so no server is needed."""
import asyncio

from agent_auditor.sdk import Auditor, AuditorError


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p, self.text = status, payload, str(payload)

    def json(self):
        return self._p


class FakeServer:
    def __init__(self, cases, reject_runs=False):
        self.cases, self.runs, self.replay_bodies, self.reject_runs = cases, [], [], reject_runs

    def get(self, path, params=None, headers=None):
        assert path.endswith("/tests"), path
        return Resp(200, self.cases)

    def post(self, path, json=None, headers=None):
        if path == "/v1/runs":
            if self.reject_runs:
                return Resp(404, {"detail": "version not registered"})
            self.runs.append(json)
            return Resp(201, {})
        assert path.endswith("/replays"), path
        self.replay_bodies.append(json)
        return Resp(200, {"verdict": "pass", "summary": {}, "results": []})

    def close(self):
        pass


def case(cid, question):
    return {"id": cid, "input": {"question": question}}


def make(cases, **kw):
    server = FakeServer(cases, **kw)
    return Auditor(agent="bot", version="v1", http_client=server, background=False), server


def test_plain_function_gets_one_test_run_per_case_with_token_case_and_version():
    auditor, server = make([case("c1", "refund?"), case("c2", "cancel?")])

    def answer(question):
        return f"answer to {question}"

    result = auditor.replay_tests(answer, version="v2")
    assert len(server.runs) == 2
    for run, cid in zip(server.runs, ("c1", "c2")):
        assert run["environment"] == "test" and run["version"] == "v2"
        assert run["tags"]["case_id"] == cid and run["tags"]["replay_token"]
        assert run["input"] == {"question": "refund?" if cid == "c1" else "cancel?"}
        assert run["output"].startswith("answer to")
    body = server.replay_bodies[0]
    assert body["version"] == "v2" and body["case_ids"] == ["c1", "c2"]
    assert body["token"] == server.runs[0]["tags"]["replay_token"] == server.runs[1]["tags"]["replay_token"]
    assert result["verdict"] == "pass" and result["client_errors"] == {} and result["flushed"] is True


def test_decorated_agent_is_not_double_recorded_and_production_is_untouched():
    auditor, server = make([case("c1", "refund?")])

    @auditor.observe_run()
    def answer(question):
        with auditor.current_run().step("kb", kind="retrieval") as s:
            s.set(num_docs=2)
        return "ok"

    auditor.replay_tests(answer, version="v3")
    assert len(server.runs) == 1
    run = server.runs[0]
    assert run["environment"] == "test" and run["version"] == "v3" and run["tags"]["case_id"] == "c1"
    assert [s["name"] for s in run["steps"]] == ["kb"]
    answer("normal call")  # outside replay: back to the auditor's own environment, no replay tags
    normal = server.runs[-1]
    assert normal["environment"] == "production" and "replay_token" not in normal["tags"] and normal["version"] == "v1"


def test_only_the_first_run_is_tagged_so_a_case_never_matches_two_runs():
    auditor, server = make([case("c1", "q")])

    @auditor.observe_run()
    def inner(x):
        return x

    @auditor.observe_run()
    def outer(question):
        inner(question)
        return "done"

    auditor.replay_tests(outer, version="v2")
    tagged = [r for r in server.runs if "replay_token" in r["tags"]]
    assert len(server.runs) == 2 and len(tagged) == 1
    assert all(r["environment"] == "test" for r in server.runs)


def test_a_crashing_agent_is_recorded_as_a_failed_run_and_replay_continues():
    auditor, server = make([case("c1", "boom"), case("c2", "fine")])

    def answer(question):
        if question == "boom":
            raise RuntimeError("tool exploded")
        return "ok"

    result = auditor.replay_tests(answer, version="v2")
    assert [r["status"] for r in server.runs] == ["error", "ok"]
    assert "tool exploded" in server.runs[0]["error"]
    assert "c1" in result["client_errors"] and "c2" not in result["client_errors"]


def test_async_agents_work():
    auditor, server = make([case("c1", "q")])

    async def answer(question):
        await asyncio.sleep(0)
        return "async ok"

    auditor.replay_tests(answer, version="v2")
    assert server.runs[0]["output"] == "async ok" and server.runs[0]["environment"] == "test"


def test_input_shapes_args_kwargs_non_dict_and_custom_call():
    cases = [
        {"id": "a", "input": {"args": ["x"], "kwargs": {"n": 2}}},   # fallback shape from _call_input
        {"id": "b", "input": "plain string"},                         # non-dict: one positional argument
    ]
    auditor, server = make(cases)

    def agent(*args, **kwargs):
        return f"{args}|{kwargs}"

    auditor.replay_tests(agent, version="v2")
    assert server.runs[0]["output"] == "('x',)|{'n': 2}"
    assert server.runs[1]["output"] == "('plain string',)|{}"

    auditor2, server2 = make([{"id": "c", "input": {"weird": 1}}])
    auditor2.replay_tests(lambda q: q, version="v2", call=lambda inp: f"custom {inp['weird']}")
    assert server2.runs[0]["output"] == "custom 1"

    auditor3, server3 = make([{"id": "d", "input": {"weird": 1}}])
    result = auditor3.replay_tests(lambda q: q, version="v2")  # input does not match parameters
    assert "does not match" in result["client_errors"]["d"]


def test_case_selection_and_errors():
    auditor, server = make([case("c1", "a"), case("c2", "b"), case("c3", "c")])
    auditor.replay_tests(lambda question: "x", version="v2", case_ids=["c3", "c1"])
    assert [r["tags"]["case_id"] for r in server.runs] == ["c1", "c3"]
    assert server.replay_bodies[0]["case_ids"] == ["c1", "c3"]
    for bad in ({"case_ids": ["nope"]}, ):
        try:
            auditor.replay_tests(lambda question: "x", **bad)
        except ValueError:
            continue
        raise AssertionError("expected ValueError")
    empty, _ = make([])
    try:
        empty.replay_tests(lambda question: "x")
    except AuditorError:
        return
    raise AssertionError("expected AuditorError")


def test_rejected_runs_are_reported_so_missing_cases_are_explained():
    auditor, server = make([case("c1", "q")], reject_runs=True)
    result = auditor.replay_tests(lambda question: "x", version="not-registered")
    assert result["runs_not_delivered"] == 1
