"""Send OpenTelemetry traces from two versions of an agent to Agent Auditor, then ask for a canary verdict.

  pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
  agent-auditor serve --api-key demo-key          (in another window)
  python examples/otel_to_auditor.py --key demo-key

Nothing here uses Agent Auditor's own SDK for recording: the "agent" is plain OpenTelemetry code that emits the span
attributes OpenInference and the OTel GenAI conventions define. The Auditor receives them on /v1/traces, turns each trace
into a run, and the canary compares version v1 (production) with v2 (canary). v2 is a fake model that answers
"I am not sure." too often, which is the kind of regression a speed check cannot see.
"""
from __future__ import annotations

import argparse
import json
import random
import time

QUESTIONS = [
    ("How long do refunds take?", "Refunds take 5 business days."),
    ("Can I change my shipping address?", "Yes, before the order ships, from your account page."),
    ("Do you ship abroad?", "We ship to 30 countries; see the shipping page."),
    ("How do I reset my password?", "Use the 'Forgot password' link on the sign-in page."),
    ("What is your return window?", "You can return items within 30 days."),
    ("Is there a student discount?", "Yes, 10 percent with a valid student email."),
]


def _provider(url: str, key: str, service: str, version: str, environment: str):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    provider = TracerProvider(resource=Resource.create({
        "service.name": service, "service.version": version, "deployment.environment.name": environment}))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=url.rstrip("/") + "/v1/traces", headers={"x-api-key": key})))
    return provider


def _one_call(tracer, question: str, answer: str, model: str, pause: float) -> None:
    with tracer.start_as_current_span("support_bot.run", attributes={
            "openinference.span.kind": "AGENT", "input.value": question, "output.value": answer}):
        with tracer.start_as_current_span("kb_search", attributes={
                "openinference.span.kind": "RETRIEVER", "retrieval.documents.0.document.content": answer,
                "retrieval.documents.0.document.score": round(random.uniform(0.7, 0.95), 2)}):
            time.sleep(pause)
        with tracer.start_as_current_span(f"chat {model}", attributes={
                "gen_ai.operation.name": "chat", "gen_ai.request.model": model, "gen_ai.usage.input_tokens": 120,
                "gen_ai.usage.output_tokens": len(answer.split()),
                "gen_ai.input.messages": json.dumps([{"role": "user", "content": question}]),
                "gen_ai.output.messages": json.dumps([{"role": "assistant", "content": answer}])}):
            time.sleep(pause)


def run(url: str, key: str, agent: str = "otel-demo", runs: int = 30, pause: float = 0.005) -> dict:
    """Emit `runs` calls from each version, then return the canary report."""
    from agent_auditor.sdk import Auditor

    rng = random.Random(7)
    for version, environment, model, lazy in (("v1", "production", "model-a", 0.03), ("v2", "canary", "model-b", 0.7)):
        provider = _provider(url, key, agent, version, environment)
        tracer = provider.get_tracer("otel_to_auditor")
        for i in range(runs):
            question, answer = QUESTIONS[i % len(QUESTIONS)]
            _one_call(tracer, question, "I am not sure." if rng.random() < lazy else answer, model, pause)
        provider.shutdown()
    return Auditor(url, agent=agent, api_key=key).evaluate_canary(
        baseline="v1", candidate="v2", since_hours=1.0, policy={"min_canary_runs": runs, "min_baseline_runs": runs})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--key", default="", help="the Auditor's API key, if it has one")
    ap.add_argument("--agent", default="otel-demo")
    ap.add_argument("--runs", type=int, default=30)
    args = ap.parse_args(argv)
    try:
        import opentelemetry.sdk  # noqa: F401
        import opentelemetry.exporter.otlp.proto.http  # noqa: F401
    except ImportError:
        print("This example needs: pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http")
        return 2
    print(f"Sending {args.runs} traces from v1 (production) and {args.runs} from v2 (canary) to {args.url} ...")
    report = run(args.url, args.key, args.agent, args.runs)
    print(f"\nVERDICT for v2: {report['verdict'].upper()}")
    for reason in report["reasons"]:
        print("  -", reason)
    print(f"\nOpen {args.url}/dashboard and pick agent '{args.agent}' to see the imported runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
