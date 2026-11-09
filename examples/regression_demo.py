"""Phase 5 demo: turn production failures into regression tests, then check candidate versions.

A support bot (v1) fails in two ways in production: its knowledge base has nothing about refunds
or chargebacks (empty retrieval), and its payments tool sometimes times out. Agent Auditor:
  1. scores the production runs,
  2. converts the failures into a small, de-duplicated set of regression tests,
  3. replays those tests against three candidate versions and grades each one.
v2 fixes retrieval only, v3 fixes retrieval and the timeout, v4 "fixes" both but breaks refunds again.

1) Start the server:   uvicorn agent_auditor.api.main:app --reload
2) Run this script:    python examples/regression_demo.py
Each run uses a fresh agent name, so earlier runs of the demo never interfere.
"""
import os
import random
import time

from agent_auditor.sdk import Auditor

URL = os.environ.get("AUDITOR_URL", "http://127.0.0.1:8000")
HEADERS = {"X-API-Key": os.environ["AUDITOR_API_KEY"]} if os.environ.get("AUDITOR_API_KEY") else {}
HEALTHY = ["reset my password", "change my email address", "cancel my plan", "get an invoice copy", "update my card"]
REFUND_QUESTIONS = [
    "How do I get a refund for my payment?", "how can I get a refund for my payment", "Where is my refund for the cancelled order?",
    "I want a chargeback on a duplicate payment", "how do i dispute a payment charge",
]
CHARGE = "Please charge my card again for the failed renewal"

# What each version of the bot can do. The only differences between versions are these switches.
VERSIONS = {
    "v1": {"kb_has_refunds": False, "payments_flaky": True},
    "v2": {"kb_has_refunds": True, "payments_flaky": True},
    "v3": {"kb_has_refunds": True, "payments_flaky": False},
    "v4": {"kb_has_refunds": False, "payments_flaky": False},
}


def build_agent(auditor: Auditor, version: str):
    """The agent under test. In real life this is your own code; here its behaviour depends on `version`."""
    caps = VERSIONS[version]

    @auditor.observe(kind="retrieval")
    def kb_search(query: str) -> list[dict]:
        wants_refund = any(w in query.lower() for w in ("refund", "chargeback", "dispute"))
        if wants_refund and not caps["kb_has_refunds"]:
            return []
        return [{"title": f"Help article: {query[:30]}"}, {"title": "Account overview"}]

    @auditor.observe(kind="tool")
    def payments_api(question: str) -> str:
        if "renewal" in question.lower() and caps["payments_flaky"]:
            raise TimeoutError("payments api timed out")
        return "ok"

    @auditor.observe_run(name="support-bot")
    def answer(question: str) -> str:
        docs = kb_search(question)
        payments_api(question)
        return "I could not find anything on that." if not docs else f"See: {docs[0]['title']}"

    return answer


def show(title: str, result: dict) -> None:
    s = result["summary"]
    print(f"\n{title}: verdict {result['verdict'].upper()}   passed {s['passed']}/{s['total']}"
          f"   fixed {len(s['fixed'])}   still failing {len(s['still_failing'])}   newly failing {len(s['newly_failing'])}")
    for r in result["results"]:
        failing = [c["check"] + ": " + c["reason"] for c in r["checks"] if c["status"] != "pass"]
        print(f"   {r['outcome']:<10} case {r['case_id'][:8]}  {'; '.join(failing)[:90]}")
    if result["client_errors"]:
        print("   agent raised:", result["client_errors"])


def main() -> None:
    random.seed(5)
    name = f"regression-demo-{int(time.time())}"
    auditor = Auditor(URL, agent=name, version="v1", api_key=os.environ.get("AUDITOR_API_KEY"))
    for label in VERSIONS:
        auditor.register_version(label, prompt=f"demo {label}", config=VERSIONS[label], use=(label == "v1"), activate=(label == "v1"))

    # 1) production traffic on v1
    prod = build_agent(auditor, "v1")
    for _ in range(40):
        try:
            prod(random.choice(HEALTHY))
        except TimeoutError:
            pass
    for q in REFUND_QUESTIONS * 3:          # many refund questions, several phrased almost identically
        prod(q)
    for _ in range(4):
        try:
            prod(CHARGE)
        except TimeoutError:
            pass
    auditor.flush()
    print(f"production: {40 + len(REFUND_QUESTIONS) * 3 + 4} runs sent as agent '{name}'")

    # 2) failures -> tests
    report = auditor.generate_tests(limit=20)
    print(f"\nconverted {report['scanned_failed_runs']} failed runs into {len(report['created'])} regression tests "
          f"({report['merged_duplicates']} duplicates merged)")
    for case in report["created"]:
        print(f"   {case['occurrences']}x  {case['name']}")

    # 3) replay candidates
    for version in ("v2", "v3", "v4"):
        agent_fn = build_agent(auditor, version)
        show(f"candidate {version}", auditor.replay_tests(agent_fn, version=version, label=f"demo {version}"))
    print(f"\nBrowse it at {URL}/docs  (GET /v1/agents/{name}/tests and /v1/agents/{name}/replays)")


if __name__ == "__main__":
    main()
