"""Skipped automatically unless langgraph is installed: pip install -e ".[dev,langgraph]" """
import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain_core")

from typing import TypedDict  # noqa: E402

from langgraph.graph import END, START, StateGraph  # noqa: E402

from agent_auditor.sdk import Auditor  # noqa: E402
from agent_auditor.sdk.langgraph import AuditorCallbackHandler  # noqa: E402


class _Resp:
    status_code = 201
    text = ""

    def json(self):
        return {}


class _Capture:
    def __init__(self):
        self.bodies = []

    def post(self, path, json=None, headers=None):
        self.bodies.append(json)
        return _Resp()

    def close(self):
        pass


class State(TypedDict, total=False):
    question: str
    answer: str


def test_graph_invocation_becomes_one_run_with_node_steps():
    def retrieve(state):
        return {}

    def generate(state):
        return {"answer": "hi"}

    b = StateGraph(State)
    b.add_node("retrieve", retrieve)
    b.add_node("generate", generate)
    b.add_edge(START, "retrieve")
    b.add_edge("retrieve", "generate")
    b.add_edge("generate", END)
    graph = b.compile()

    cap = _Capture()
    auditor = Auditor(agent="g", http_client=cap, background=False)
    handler = AuditorCallbackHandler(auditor, node_kinds={"retrieve": "retrieval"})
    graph.invoke({"question": "q"}, config={"callbacks": [handler]})

    assert len(cap.bodies) == 1
    steps = {s["name"]: s["kind"] for s in cap.bodies[0]["steps"]}
    assert steps.get("retrieve") == "retrieval" and "generate" in steps
    assert cap.bodies[0]["status"] == "ok"
