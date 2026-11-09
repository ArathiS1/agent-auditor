"""Phase 3 demo: catch a regression between two agent versions.

Simulates a support bot. v1 is healthy. v2 is a "prompt change" that makes retrieval miss more
often and return weaker matches. We score the runs, then ask Agent Auditor to compare them.

1) Start the server:   uvicorn agent_auditor.api.main:app --reload
2) Run this script:    python examples/eval_demo.py
   (Delete agent_auditor.db first if you want a clean slate; re-runs add more runs.)
"""
import os
import random

from agent_auditor.sdk import Auditor

URL = os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000")
HEADERS = {"X-API-Key": os.environ["AUDITOR_API_KEY"]} if os.environ.get("AUDITOR_API_KEY") else {}
random.seed(7)

auditor = Auditor(URL, agent="eval-demo", background=False, api_key=os.environ.get("AUDITOR_API_KEY"))
auditor.register_version("v1", prompt="Answer only from the retrieved articles.", config={"top_k": 4})
auditor.register_version("v2", prompt="Answer briefly.", config={"top_k": 1}, activate=False, use=False)

TOPICS = ["reset your password", "change your email", "cancel your plan", "download an invoice"]


def simulate(version: str, miss_rate: float, weak_rate: float, n: int) -> None:
    for _ in range(n):
        topic = random.choice(TOPICS)
        with auditor.run(input=f"How do I {topic}?", version=version) as run:
            missed = random.random() < miss_rate
            weak = random.random() < weak_rate
            docs = [] if missed else [f"Help article: how to {topic}", "Account settings overview"]
            with run.step("kb_search", kind="retrieval", input=topic) as s:
                s.output = docs
                s.set(scores=[] if missed else ([0.35] if weak else [round(random.uniform(0.7, 0.95), 2)]))
            with run.step("answer", kind="llm"):
                pass
            run.output = "I could not find that." if missed else f"To {topic}, open the help article on {topic}."


simulate("v1", miss_rate=0.05, weak_rate=0.05, n=60)
simulate("v2", miss_rate=0.35, weak_rate=0.25, n=60)

print("scoring runs ->", auditor.run_evals(limit=500))

result = auditor.compare_versions("v1", "v2")
print(f"\noverall verdict: {result['overall_verdict'].upper()}")
for row in result["scorers"]:
    if row["delta"] is None:
        print(f"  {row['scorer']:<22} {row['verdict']:<18} {row['reason']}")
    else:
        print(f"  {row['scorer']:<22} {row['verdict']:<18} {row['baseline_pass_rate']:.0%} -> {row['candidate_pass_rate']:.0%}  (p={row['p_value']:.3g})")
for note in result["notes"]:
    print("  note:", note)
print("\nBrowse the same data at http://127.0.0.1:8000/docs  (GET /v1/agents/eval-demo/eval-summary)")
