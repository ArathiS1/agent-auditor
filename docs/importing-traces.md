# Importing OpenTelemetry traces

If your agent already emits OpenTelemetry traces, you do not need to change its code to use Agent Auditor. Point the exporter
at the Auditor. Each trace becomes one run, its model, tool and retrieval spans become steps, and everything after that (evals,
root causes, regression tests, canary, releases) works on it.

## Quick start

```bash
pip install "agent-auditor[otel]"          # the [otel] part is only needed for protobuf, which most exporters send
agent-auditor serve --api-key demo-key

export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:8000     # the exporter adds /v1/traces
export OTEL_EXPORTER_OTLP_HEADERS="x-api-key=demo-key"
```

Run your agent as usual. Then open `http://127.0.0.1:8000/dashboard` and pick the agent. Without an API key on the server, leave
the header out.

To see it work without your own agent: `pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http`, then
`python examples/otel_to_auditor.py --key demo-key`. It emits traces from two versions with plain OpenTelemetry code, and the
canary rolls the second one back because it answers "I am not sure." too often.

## How a trace maps to a run

| OpenTelemetry | Agent Auditor |
|---|---|
| `service.name` (resource) | the agent. Override with `x-auditor-agent` header or `?agent=` |
| `service.version` (resource) | the version label. Registered automatically if it is new. Override with `x-auditor-version` or `?version=` |
| `deployment.environment.name` or `deployment.environment` | the environment: `canary` stays `canary`; test, staging, dev, ci and local become `test`; anything else is `production`. Override with `x-auditor-environment` or `?environment=` |
| a trace | one run. The trace id is the run id, so sending a trace twice does not duplicate it |
| the root span (no parent) | the run: its start and end give the latency, its status gives ok or error, its input and output give the question and answer |
| child spans that call a model, a tool or a retriever | steps (`llm`, `tool`, `retrieval`) |
| child spans that only group others (a "chain" or "agent" span with children) | left out |
| other leaf spans (an HTTP call, a database query) | steps of kind `other`, so their time still counts |
| an exception event on a span | the error text |

Where the question, answer, model and tokens are read from:

| Convention | What is read |
|---|---|
| OpenInference | `openinference.span.kind`, `input.value`, `output.value`, `llm.input_messages.N.*`, `llm.output_messages.N.*`, `llm.model_name`, `llm.token_count.*`, `retrieval.documents.N.document.content` and `.score` |
| OTel GenAI | `gen_ai.operation.name`, `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.request.model`, `gen_ai.system`, `gen_ai.usage.*`, `gen_ai.tool.call.arguments` and `.result` |
| OpenLLMetry (Traceloop) | `traceloop.span.kind`, `traceloop.entity.input` and `.output`, `gen_ai.prompt.N.*`, `gen_ai.completion.N.*` |
| Langfuse (OTel) | `langfuse.observation.type`, `langfuse.observation.input` and `.output`, `langfuse.trace.input` and `.output` |

If the root span carries no question or answer, the run takes them from its first and last model call. Retrieval spans
with documents and scores get `num_docs` and `scores`, so the retrieval checks apply to imported runs. A span with none of
these attributes still imports, with its timing and status. Nothing is guessed: a field that is not found stays empty.

## Traces that arrive in pieces

Exporters usually send a span as soon as it ends, so the children of a trace arrive before its root. The Auditor keeps
the run marked `otel.root_pending` until the root arrives, then fills in the question, answer, status and latency and
removes the mark. A step is never added twice, so resending is safe.

## Saved traces

```bash
agent-auditor import-otlp traces.json --url http://127.0.0.1:8000 --api-key demo-key
```

The file is OTLP/JSON: one request, or one request per line (what the OpenTelemetry Collector's `file` exporter writes).
`--agent`, `--environment` and `--version` override what is in the file.

## Using the data

```python
auditor.run_evals()                                   # or let a canary evaluate the runs it compares
auditor.evaluate_canary(baseline="v1", candidate="v2")
```

Imported runs have no expected answers, so the checks that apply are the ones that need none: errors, speed, retrieval,
`gives_an_answer` and, if you turn it on, `answer_judge` (see [answer-correctness.md](answer-correctness.md)).
Imported runs do not pick up expected answers from span attributes, so `expectations_met` does not apply to them.

## Limits

- **Protobuf needs the extra.** JSON works without it. `pip install "agent-auditor[otel]"` adds protobuf decoding. A server
  without it answers protobuf with a clear 415 error that says so. Exporters that use gRPC cannot send to this endpoint;
  use the HTTP exporter (`OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`).
- **A trace that crosses services becomes one run per service.** The one without the root span stays marked `otel.root_pending`.
- **Mapping is by convention.** Instrumentation that uses other attribute names imports with timing and status but no
  question or answer text. If yours does, open an issue with a sample span and the mapping is a few lines.
- **Metrics and logs are ignored.** Only traces are received.
- **Not tested against every exporter.** It was tested with the OpenTelemetry Python SDK sending protobuf over HTTP, and
  with hand-written JSON in the OpenInference, GenAI, OpenLLMetry and Langfuse styles. Not yet with a Collector, the
  JavaScript or Java exporters, or a Langfuse deployment.
- **No sampling or retention controls.** Everything sent is stored. Limit what you send at the exporter or the Collector.
- Requests are capped at 32 MB and 50,000 spans.
