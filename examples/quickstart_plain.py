"""Send realistic fake traces from a plain-Python "agent" (no LLM key, no extra libraries).

1) Start the server:   uvicorn agent_auditor.api.main:app --reload
2) In another terminal: python examples/quickstart_plain.py
3) Look at http://127.0.0.1:8000/docs -> GET /v1/runs  (try status=error)
"""
import os
import random

from agent_auditor.sdk import Auditor

auditor = Auditor(os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000"), agent="support-bot", api_key=os.environ.get("AUDITOR_API_KEY"))
auditor.register_version(
    "v1", prompt="Answer using only the retrieved articles.", config={"top_k": 4, "model": "demo-model"}
)

QUESTIONS = ["reset password", "change email", "cancel plan", "invoice copy", "delete account", "update card"]


@auditor.observe(kind="retrieval")
def kb_search(query: str) -> list[dict]:
    if random.random() < 0.2:
        return []  # a retrieval miss: nothing found
    return [{"title": f"How to {query}", "score": round(random.uniform(0.5, 0.95), 2)} for _ in range(3)]


@auditor.observe(kind="tool")
def lookup_account(user_id: int) -> dict:
    if random.random() < 0.1:
        raise TimeoutError("account service timed out")
    return {"user_id": user_id, "plan": "pro"}


@auditor.observe_run(name="support-bot")
def answer(question: str, user_id: int) -> str:
    docs = kb_search(question)
    lookup_account(user_id)
    return "I couldn't find anything on that." if not docs else f"See: {docs[0]['title']}"


for i in range(30):
    try:
        answer(random.choice(QUESTIONS), user_id=i)
    except TimeoutError:
        pass  # the failed run is already recorded with status=error

auditor.flush()
print("sent:", auditor.stats)
