from agent_auditor.sdk._mapper import CallbackMapper


class FakeAuditor:
    def __init__(self):
        self.sent = []
        self.runs = []

    def run(self, input=None, tags=None, **kw):
        from agent_auditor.sdk import Auditor

        real = Auditor(agent="bot", http_client=_Capture(self.sent), background=False)
        run = real.run(input=input, tags=tags)
        self.runs.append(run)
        return run


class _Resp:
    status_code = 201
    text = ""

    def json(self):
        return {}


class _Capture:
    def __init__(self, sink):
        self.sink = sink

    def post(self, path, json=None, headers=None):
        self.sink.append(json)
        return _Resp()

    def close(self):
        pass


def graph_run(mapper):
    """root chain -> node 'retrieve' (with a retriever child) -> node 'generate' (with an llm child)."""
    mapper.start(kind="chain", run_id="root", parent_run_id=None, name="LangGraph", input={"q": "hi"})
    mapper.start(kind="chain", run_id="n1", parent_run_id="root", name="retrieve", metadata={"langgraph_node": "retrieve"})
    mapper.start(kind="retrieval", run_id="r1", parent_run_id="n1", name="kb", input="hi")
    mapper.end(run_id="r1", output=[{"c": 1}], attributes={"scores": [0.4]})
    mapper.end(run_id="n1", output={"docs": 1})
    mapper.start(kind="chain", run_id="plumb", parent_run_id="root", name="RunnableSequence", metadata={})
    mapper.start(kind="chain", run_id="n2", parent_run_id="plumb", name="generate", metadata={"langgraph_node": "generate"})
    mapper.start(kind="llm", run_id="l1", parent_run_id="n2", name="chat", input=["hi"])
    mapper.end(run_id="l1", output="answer")
    mapper.end(run_id="n2", output={"answer": "answer"})
    mapper.end(run_id="plumb")
    mapper.end(run_id="root", output={"answer": "answer"})


def test_one_run_with_node_and_leaf_steps():
    fake = FakeAuditor()
    mapper = CallbackMapper(fake, node_kinds={"retrieve": "retrieval", "generate": "llm"})
    graph_run(mapper)
    assert len(fake.sent) == 1
    body = fake.sent[0]
    assert body["output"] == {"answer": "answer"} and body["status"] == "ok"
    names = [(s["name"], s["kind"]) for s in body["steps"]]
    assert names == [("retrieve", "retrieval"), ("kb", "retrieval"), ("generate", "llm"), ("chat", "llm")]
    assert "RunnableSequence" not in [n for n, _ in names]
    kb = next(s for s in body["steps"] if s["name"] == "kb")
    assert kb["attributes"]["scores"] == [0.4]


def test_nodes_can_be_excluded():
    fake = FakeAuditor()
    mapper = CallbackMapper(fake, include_nodes=False)
    graph_run(mapper)
    assert [s["name"] for s in fake.sent[0]["steps"]] == ["kb", "chat"]


def test_error_in_child_fails_run_and_step():
    fake = FakeAuditor()
    mapper = CallbackMapper(fake)
    mapper.start(kind="chain", run_id="root", parent_run_id=None, name="G")
    mapper.start(kind="tool", run_id="t1", parent_run_id="root", name="api")
    mapper.error(run_id="t1", error=TimeoutError("slow"))
    mapper.error(run_id="root", error=RuntimeError("graph failed"))
    body = fake.sent[0]
    assert body["status"] == "error" and "graph failed" in body["error"]
    assert body["steps"][0]["status"] == "error"


def test_state_is_cleaned_up_and_runs_are_independent():
    fake = FakeAuditor()
    mapper = CallbackMapper(fake)
    for i in range(3):
        mapper.start(kind="chain", run_id=f"root{i}", parent_run_id=None, name="G")
        mapper.start(kind="llm", run_id=f"l{i}", parent_run_id=f"root{i}", name="chat")
        mapper.end(run_id=f"l{i}", output="x")
        mapper.end(run_id=f"root{i}", output="y")
    assert len(fake.sent) == 3
    assert not mapper._roots and not mapper._root_of and not mapper._members and not mapper._steps


def test_orphan_child_starts_its_own_run():
    fake = FakeAuditor()
    mapper = CallbackMapper(fake)
    mapper.start(kind="llm", run_id="solo", parent_run_id="unknown-parent", name="chat", input="hi")
    mapper.end(run_id="solo", output="hello")
    assert len(fake.sent) == 1 and fake.sent[0]["output"] == "hello"
