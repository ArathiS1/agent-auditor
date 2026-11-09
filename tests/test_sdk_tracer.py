import asyncio

from agent_auditor.sdk import Auditor, AuditorError


class FakeResponse:
    def __init__(self, status_code=201, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, status_code=201, raises=False):
        self.posts = []
        self.status_code = status_code
        self.raises = raises

    def post(self, path, json=None, headers=None):
        if self.raises:
            raise ConnectionError("server down")
        self.posts.append((path, json, headers))
        if path.endswith("/versions"):
            return FakeResponse(self.status_code, {"label": json["label"]})
        return FakeResponse(self.status_code, {})

    def close(self):
        pass


def make(**kw):
    http = kw.pop("http", None) or FakeHttp()
    return Auditor(agent="bot", http_client=http, background=False, **kw), http


def test_run_context_sends_payload_with_ordered_steps():
    auditor, http = make(api_key="k")
    with auditor.run(input={"q": "hi"}, tags={"user": "u1"}) as run:
        with run.step("search", kind="retrieval", input="hi") as s:
            s.output = ["a", "b"]
            s.set(scores=[0.9, 0.2])
        with run.step("answer", kind="llm"):
            pass
        run.output = "done"
    path, body, headers = http.posts[0]
    assert path == "/v1/runs" and headers == {"X-API-Key": "k"}
    assert body["agent"] == "bot" and body["status"] == "ok" and body["output"] == "done"
    assert [s["name"] for s in body["steps"]] == ["search", "answer"]
    assert body["steps"][0]["attributes"]["scores"] == [0.9, 0.2]
    assert body["tags"] == {"user": "u1"}
    assert auditor.stats["sent"] == 1


def test_exception_marks_run_error_and_propagates():
    auditor, http = make()
    try:
        with auditor.run(input="x"):
            raise ValueError("boom")
    except ValueError:
        pass
    else:
        raise AssertionError("exception must propagate")
    body = http.posts[0][1]
    assert body["status"] == "error" and "ValueError: boom" in body["error"]


def test_step_exception_marks_only_the_step():
    auditor, http = make()
    with auditor.run() as run:
        try:
            with run.step("tool", kind="tool"):
                raise TimeoutError("slow")
        except TimeoutError:
            pass
    body = http.posts[0][1]
    assert body["status"] == "ok"
    assert body["steps"][0]["status"] == "error" and "TimeoutError" in body["steps"][0]["error"]


def test_unknown_step_kind_becomes_other():
    auditor, http = make()
    with auditor.run() as run:
        with run.step("x", kind="planner"):
            pass
    step = http.posts[0][1]["steps"][0]
    assert step["kind"] == "other" and step["attributes"]["original_kind"] == "planner"


def test_observe_records_step_only_inside_a_run():
    auditor, http = make()

    @auditor.observe(kind="tool")
    def add(a, b=1):
        return a + b

    assert add(1) == 2  # outside a run: no-op, nothing sent
    assert http.posts == []
    with auditor.run():
        assert add(2, b=3) == 5
    step = http.posts[0][1]["steps"][0]
    assert step["name"] == "add" and step["kind"] == "tool"
    assert step["input"] == {"a": 2, "b": 3} and step["output"] == 5


def test_observe_run_records_whole_call_and_errors():
    auditor, http = make()

    @auditor.observe_run(name="agent")
    def agent(q):
        if q == "bad":
            raise RuntimeError("nope")
        return q.upper()

    assert agent("hi") == "HI"
    try:
        agent("bad")
    except RuntimeError:
        pass
    ok, err = http.posts[0][1], http.posts[1][1]
    assert ok["output"] == "HI" and ok["tags"]["function"] == "agent"
    assert err["status"] == "error"


def test_async_observe_and_observe_run():
    auditor, http = make()

    @auditor.observe(kind="tool")
    async def fetch(x):
        return x * 2

    @auditor.observe_run()
    async def agent(x):
        return await fetch(x)

    assert asyncio.run(agent(4)) == 8
    body = http.posts[0][1]
    assert body["output"] == 8 and body["steps"][0]["name"] == "fetch"


def test_send_failure_never_raises_and_is_counted():
    auditor, _ = make(http=FakeHttp(raises=True))
    with auditor.run():
        pass
    assert auditor.stats["failed"] == 1

    auditor2, _ = make(http=FakeHttp(status_code=500))
    with auditor2.run():
        pass
    assert auditor2.stats["failed"] == 1


def test_register_version_sets_label_and_raises_on_failure():
    auditor, http = make()
    auditor.register_version("v1", prompt="p", config={"k": 1})
    assert http.posts[0][0] == "/v1/agents/bot/versions" and auditor.version == "v1"
    with auditor.run():
        pass
    assert http.posts[1][1]["version"] == "v1"

    bad, _ = make(http=FakeHttp(status_code=409))
    try:
        bad.register_version("v1")
    except AuditorError:
        pass
    else:
        raise AssertionError("expected AuditorError")

    down, _ = make(http=FakeHttp(raises=True))
    try:
        down.register_version("v1")
    except AuditorError:
        pass
    else:
        raise AssertionError("expected AuditorError")


def test_agent_name_is_url_encoded():
    auditor, http = make()
    auditor.register_version("v1", agent="my bot/1")
    assert http.posts[0][0] == "/v1/agents/my%20bot%2F1/versions"


def test_disabled_auditor_is_a_noop():
    auditor = Auditor(agent="bot", enabled=False)
    with auditor.run() as run:
        run.step("x").end()
    assert auditor.stats == {"sent": 0, "failed": 0, "dropped": 0}
    assert auditor.register_version("v1") == {}


def test_background_sender_flushes():
    http = FakeHttp()
    auditor = Auditor(agent="bot", http_client=http)
    for _ in range(5):
        with auditor.run():
            pass
    assert auditor.flush(timeout=5)
    assert auditor.stats["sent"] == 5 and len(http.posts) == 5
    auditor.close()


def test_queue_full_drops_instead_of_blocking():
    import threading

    gate = threading.Event()

    class SlowHttp(FakeHttp):
        def post(self, path, json=None, headers=None):
            gate.wait(5)
            return super().post(path, json=json, headers=headers)

    auditor = Auditor(agent="bot", http_client=SlowHttp(), max_queue=1)
    for _ in range(6):
        with auditor.run():
            pass
    assert auditor.stats["dropped"] >= 1
    gate.set()
    auditor.close()


def test_invalid_environment_rejected():
    try:
        Auditor(agent="bot", http_client=FakeHttp(), background=False, environment="staging")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_finish_is_idempotent():
    auditor, http = make()
    run = auditor.run()
    run.finish()
    run.finish()
    assert len(http.posts) == 1
