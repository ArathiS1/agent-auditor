"""A tiny LangGraph agent (no LLM key needed) traced by Agent Auditor.

    pip install -e ".[dev,langgraph]"
    uvicorn agent_auditor.api.main:app --reload      # in another terminal
    python examples/langgraph_demo.py
"""
import os
import random
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agent_auditor.sdk import Auditor
from agent_auditor.sdk.langgraph import AuditorCallbackHandler


class State(TypedDict, total=False):
    question: str
    docs: list[dict]
    answer: str


def retrieve(state: State) -> State:
    if random.random() < 0.25:
        return {"docs": []}  # retrieval miss
    return {"docs": [{"title": "Reset your password", "score": round(random.uniform(0.5, 0.95), 2)}]}


def generate(state: State) -> State:
    if not state["docs"]:
        return {"answer": "I don't know."}
    return {"answer": f"See: {state['docs'][0]['title']}"}


builder = StateGraph(State)
builder.add_node("retrieve", retrieve)
builder.add_node("generate", generate)
builder.add_edge(START, "retrieve")
builder.add_edge("retrieve", "generate")
builder.add_edge("generate", END)
graph = builder.compile()

auditor = Auditor(os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000"), agent="langgraph-demo", api_key=os.environ.get("AUDITOR_API_KEY"))
auditor.register_version("v1", prompt="demo graph", config={"nodes": ["retrieve", "generate"]})
handler = AuditorCallbackHandler(auditor, node_kinds={"retrieve": "retrieval", "generate": "llm"})

for i in range(10):
    graph.invoke({"question": f"question {i}"}, config={"callbacks": [handler]})

auditor.flush()
print("sent:", auditor.stats)
