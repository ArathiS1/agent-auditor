"""OpenTelemetry import: the span-to-run mapping, the /v1/traces endpoint, merging traces that arrive in pieces,
the protobuf path (skipped if the OpenTelemetry packages are not installed) and the file-import command."""
import gzip
import json

import pytest

from agent_auditor import cli
from agent_auditor.importers import otel

T1 = "0af7651916cd43dd8448eb211c80319c"
NS = 1_700_000_000_000_000_000


def attr(key, value):
    if isinstance(value, bool):
        v = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    else:
        v = {"stringValue": str(value)}
    return {"key": key, "value": v}


def span(sid, name, parent="", start=0, ms=100, attrs=None, error=None, trace=T1, events=None):
    s = {"traceId": trace, "spanId": sid, "name": name, "startTimeUnixNano": str(NS + start * 1_000_000),
         "endTimeUnixNano": str(NS + (start + ms) * 1_000_000), "attributes": [attr(k, v) for k, v in (attrs or {}).items()]}
    if parent:
        s["parentSpanId"] = parent
    if error:
        s["status"] = {"code": 2, "message": error}
    if events:
        s["events"] = events
    return s


def request(spans, service="support-bot", **resource):
    res = {"service.name": service, **resource}
    return {"resourceSpans": [{"resource": {"attributes": [attr(k, v) for k, v in res.items()]},
                               "scopeSpans": [{"scope": {"name": "t"}, "spans": spans}]}]}


def openinference_trace(trace=T1, question="How long do refunds take?", answer="Refunds take 5 days."):
    return [
        span("a" * 16, "support_bot.run", start=0, ms=300, trace=trace,
             attrs={"openinference.span.kind": "AGENT", "input.value": question, "output.value": answer}),
        span("b" * 16, "kb_search", parent="a" * 16, start=10, ms=50, trace=trace,
             attrs={"openinference.span.kind": "RETRIEVER", "retrieval.documents.0.document.content": "Refunds take 5 days.",
                    "retrieval.documents.0.document.score": 0.91, "retrieval.documents.1.document.content": "Contact us.",
                    "retrieval.documents.1.document.score": 0.4}),
        span("c" * 16, "chat", parent="a" * 16, start=70, ms=200, trace=trace,
             attrs={"openinference.span.kind": "LLM", "llm.model_name": "gpt-x", "llm.token_count.prompt": 120,
                    "llm.token_count.completion": 30}),
    ]


def build(spans, **kw):
    return otel.build_runs(otel.parse(request(spans, **kw.pop("resource", {}))), **kw)


# ----------------------------------------------------------------- mapping
def test_openinference_trace_becomes_a_run_with_steps():
    [run] = build(openinference_trace())
    assert run.trace_id == T1 and run.agent == "support-bot" and run.has_root
    assert run.input == "How long do refunds take?" and run.output == "Refunds take 5 days."
    assert [(s.kind, s.name) for s in run.steps] == [("retrieval", "kb_search"), ("llm", "chat")]
    retrieval, llm = run.steps
    assert retrieval.attributes["num_docs"] == 2 and retrieval.attributes["scores"] == [0.91, 0.4]
    assert retrieval.output == ["Refunds take 5 days.", "Contact us."]
    assert llm.attributes["model"] == "gpt-x" and llm.attributes["input_tokens"] == 120 and llm.attributes["output_tokens"] == 30
    assert run.tags["model"] == "gpt-x" and run.tags["source"] == "otlp" and run.status == "ok"
    assert (run.ended_at - run.started_at).total_seconds() == pytest.approx(0.3)


def test_genai_conventions_take_question_and_answer_from_the_model_call():
    msgs_in = json.dumps([{"role": "system", "content": "be brief"}, {"role": "user", "content": "Capital of France?"}])
    msgs_out = json.dumps([{"role": "assistant", "content": "Paris."}])
    spans = [span("a" * 16, "invoke_agent helper", attrs={"gen_ai.operation.name": "invoke_agent"}),
             span("b" * 16, "chat m", parent="a" * 16, start=5, attrs={
                 "gen_ai.operation.name": "chat", "gen_ai.request.model": "m", "gen_ai.system": "openai",
                 "gen_ai.input.messages": msgs_in, "gen_ai.output.messages": msgs_out,
                 "gen_ai.usage.input_tokens": 9, "gen_ai.usage.output_tokens": 2})]
    [run] = build(spans)
    assert run.input == "Capital of France?" and run.output == "Paris."
    assert run.steps[0].kind == "llm" and run.steps[0].attributes["provider"] == "openai"


def test_older_indexed_message_attributes_are_read():
    spans = [span("a" * 16, "workflow", attrs={"traceloop.span.kind": "workflow"}),
             span("b" * 16, "openai.chat", parent="a" * 16, start=5, attrs={
                 "llm.request.type": "chat", "gen_ai.prompt.0.role": "user", "gen_ai.prompt.0.content": "2+2?",
                 "gen_ai.completion.0.role": "assistant", "gen_ai.completion.0.content": "4"})]
    [run] = build(spans)
    assert run.input == "2+2?" and run.output == "4"


def test_langfuse_observation_attributes_are_read():
    spans = [span("a" * 16, "trace", attrs={"langfuse.trace.input": "hello", "langfuse.trace.output": "hi there"}),
             span("b" * 16, "gen", parent="a" * 16, start=5, attrs={"langfuse.observation.type": "generation", "langfuse.observation.input": "hello"})]
    [run] = build(spans)
    assert run.input == "hello" and run.output == "hi there" and run.steps[0].kind == "llm"


def test_errors_come_from_status_and_exception_events():
    boom = [{"name": "exception", "attributes": [attr("exception.type", "TimeoutError"), attr("exception.message", "orders api timed out")]}]
    spans = [span("a" * 16, "run", error="failed", events=boom, attrs={"openinference.span.kind": "AGENT", "input.value": "cancel"}),
             span("b" * 16, "cancel_order", parent="a" * 16, start=5, error="TimeoutError: orders api timed out",
                  attrs={"openinference.span.kind": "TOOL"})]
    [run] = build(spans)
    assert run.status == "error" and run.error == "TimeoutError: orders api timed out"  # the exception beats the bare status text
    assert run.steps[0].status == "error" and "timed out" in run.steps[0].error and run.output is None
    only_event = build([span("a" * 16, "run", events=boom)])[0]
    assert only_event.status == "error" and only_event.error == "TimeoutError: orders api timed out"


def test_spans_that_only_group_others_are_left_out_but_leaf_spans_stay():
    spans = [span("a" * 16, "root"), span("b" * 16, "plan", parent="a" * 16, attrs={"openinference.span.kind": "CHAIN"}),
             span("c" * 16, "chat", parent="b" * 16, start=5, attrs={"gen_ai.operation.name": "chat"}),
             span("d" * 16, "http GET", parent="a" * 16, start=9)]
    [run] = build(spans)
    assert [s.name for s in run.steps] == ["chat", "http GET"]


def test_environment_and_version_come_from_the_resource_and_can_be_overridden():
    [run] = build(openinference_trace(), resource={"deployment.environment.name": "canary", "service.version": "v2"})
    assert run.environment == "canary" and run.version == "v2"
    [run] = build(openinference_trace(), resource={"deployment.environment": "staging"}, environment="production", version="v9", agent="other")
    assert run.environment == "production" and run.version == "v9" and run.agent == "other"
    assert otel.map_environment("staging") == "test" and otel.map_environment("prod") == "production"


def test_unreadable_payloads_are_rejected():
    for bad in ({}, [], {"resourceSpans": "x"}, "text"):
        with pytest.raises(otel.OtlpError):
            otel.parse(bad)
    assert otel.parse({"resourceSpans": []}) == []


def test_base64_ids_from_protobuf_json_tools_are_accepted():
    import base64

    sp = span("a" * 16, "r")
    sp["traceId"] = base64.b64encode(bytes.fromhex(T1)).decode()
    sp["spanId"] = base64.b64encode(bytes.fromhex("1234567890abcdef")).decode()
    [parsed] = otel.parse(request([sp]))
    assert parsed.trace_id == T1 and parsed.span_id == "1234567890abcdef"


# ----------------------------------------------------------------- the endpoint
def post(client, payload, **kw):
    return client.post("/v1/traces", json=payload, **kw)


def runs(client, agent="support-bot"):
    return client.get("/v1/runs", params={"agent": agent, "limit": 100}).json()


def test_traces_endpoint_stores_runs_and_steps(client):
    r = post(client, request(openinference_trace(), **{"service.version": "v2", "deployment.environment.name": "canary"}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["partialSuccess"] == {} and body["imported"]["runs_created"] == 1 and body["imported"]["agents"] == ["support-bot"]
    [run] = runs(client)
    assert run["id"] == T1 and run["environment"] == "canary" and run["version"] == "v2" and run["step_count"] == 2
    detail = client.get(f"/v1/runs/{T1}").json()
    assert [s["kind"] for s in detail["steps"]] == ["retrieval", "llm"]
    assert [v["label"] for v in client.get("/v1/agents/support-bot/versions").json()] == ["v2"]


def test_sending_the_same_trace_twice_does_not_duplicate_it(client):
    payload = request(openinference_trace())
    assert post(client, payload).json()["imported"]["runs_created"] == 1
    again = post(client, payload).json()["imported"]
    assert again["runs_created"] == 0 and again["runs_updated"] == 0
    [run] = runs(client)
    assert run["step_count"] == 2


def test_a_trace_that_arrives_in_pieces_is_merged_into_one_run(client):
    root, search, chat = openinference_trace()
    for piece in ([search], [chat], [root]):  # children end first, the root last
        assert post(client, request(piece)).status_code == 200
    [run] = runs(client)
    assert run["input"] == "How long do refunds take?" and run["output"] == "Refunds take 5 days." and run["status"] == "ok"
    assert run["step_count"] == 2 and "otel.root_pending" not in run["tags"] and run["tags"]["model"] == "gpt-x"
    assert run["latency_ms"] == pytest.approx(300)
    steps = client.get(f"/v1/runs/{run['id']}").json()["steps"]
    assert [s["name"] for s in steps] == ["kb_search", "chat"] and [s["position"] for s in steps] == [0, 1]


def test_a_run_waiting_for_its_root_is_marked_and_takes_its_question_from_the_model_call(client):
    msgs = json.dumps([{"role": "user", "content": "Capital of France?"}])
    out = json.dumps([{"role": "assistant", "content": "Paris."}])
    child = span("b" * 16, "chat", parent="a" * 16, attrs={"gen_ai.operation.name": "chat", "gen_ai.input.messages": msgs,
                                                          "gen_ai.output.messages": out})
    post(client, request([child]))
    [run] = runs(client)
    assert run["tags"]["otel.root_pending"] is True and run["input"] == "Capital of France?" and run["output"] == "Paris."


def test_agent_environment_and_version_can_be_set_with_headers_or_query(client):
    h = {"x-auditor-agent": "bot-h", "x-auditor-environment": "test", "x-auditor-version": "v7"}
    post(client, request(openinference_trace()), headers=h)
    [run] = runs(client, "bot-h")
    assert run["environment"] == "test" and run["version"] == "v7"
    post(client, request(openinference_trace(trace="1" * 32)), params={"agent": "bot-q", "environment": "canary"})
    assert runs(client, "bot-q")[0]["environment"] == "canary"
    assert post(client, request(openinference_trace()), params={"environment": "nope"}).status_code == 422


def test_bad_bodies_get_clear_errors(client):
    assert post(client, {"hello": "world"}).status_code == 422
    r = client.post("/v1/traces", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 415 and "JSON" in r.json()["detail"]
    body = gzip.compress(json.dumps(request(openinference_trace(trace="2" * 32))).encode())
    r = client.post("/v1/traces", content=body, headers={"content-type": "application/json", "content-encoding": "gzip"})
    assert r.status_code == 200 and r.json()["imported"]["runs_created"] == 1
    r = client.post("/v1/traces", content=b"\x1f\x8bnope", headers={"content-type": "application/json", "content-encoding": "gzip"})
    assert r.status_code == 400


def test_the_endpoint_needs_the_api_key_when_one_is_set():
    from fastapi.testclient import TestClient

    from agent_auditor.api.app import create_app

    c = TestClient(create_app(database_url="sqlite://", api_key="secret"))
    assert c.post("/v1/traces", json=request(openinference_trace())).status_code == 401
    assert c.post("/v1/traces", json=request(openinference_trace()), headers={"X-API-Key": "secret"}).status_code == 200


def test_an_imported_non_answer_is_caught_by_the_new_check(client):
    post(client, request(openinference_trace(trace="3" * 32, answer="I am not sure.")))
    post(client, request(openinference_trace(trace="4" * 32)))
    r = client.post("/v1/evals/run", json={"agent": "support-bot", "scorers": ["gives_an_answer", "grounded_overlap", "retrieval_top_score"]})
    assert r.status_code == 200, r.text
    verdict = {rid: {e["scorer"]: e for e in client.get(f"/v1/runs/{rid}/evals").json()} for rid in ("3" * 32, "4" * 32)}
    assert verdict["3" * 32]["gives_an_answer"]["passed"] is False
    assert verdict["4" * 32]["gives_an_answer"]["passed"] is True
    assert verdict["4" * 32]["retrieval_top_score"]["passed"] is True  # the document scores came across


# ----------------------------------------------------------------- protobuf, with the real SDK's encoder
def sdk_spans():
    pytest.importorskip("opentelemetry.sdk")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.common")
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import Status, StatusCode

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "sdk-bot", "service.version": "v3"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("t")
    with tracer.start_as_current_span("run", attributes={"openinference.span.kind": "AGENT", "input.value": "q1", "output.value": "a1"}):
        with tracer.start_as_current_span("chat", attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": "m", "gen_ai.usage.input_tokens": 7}):
            pass
    with tracer.start_as_current_span("run2", attributes={"input.value": "q2"}) as bad:
        bad.set_status(Status(StatusCode.ERROR, "it broke"))
    return exporter.get_finished_spans()


def test_protobuf_from_the_real_sdk_is_imported(client):
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans

    body = encode_spans(sdk_spans()).SerializeToString()
    r = client.post("/v1/traces", content=body, headers={"content-type": "application/x-protobuf"})
    assert r.status_code == 200 and r.headers["content-type"] == "application/x-protobuf" and r.content == b""
    stored = sorted(runs(client, "sdk-bot"), key=lambda x: x["input"])
    assert [(x["input"], x["output"], x["status"], x["version"]) for x in stored] == [("q1", "a1", "ok", "v3"), ("q2", None, "error", "v3")]
    assert stored[0]["step_count"] == 1 and len(stored[0]["id"]) == 32
    step = client.get(f"/v1/runs/{stored[0]['id']}").json()["steps"][0]
    assert step["kind"] == "llm" and step["attributes"]["model"] == "m" and step["attributes"]["input_tokens"] == 7


def test_protobuf_without_the_otel_package_says_how_to_install_it(client, monkeypatch):
    import builtins

    real = builtins.__import__

    def blocked(name, *a, **k):
        if name.startswith("opentelemetry.proto"):
            raise ImportError("blocked")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)
    r = client.post("/v1/traces", content=b"\x0a\x00", headers={"content-type": "application/x-protobuf"})
    assert r.status_code == 415 and "agent-auditor[otel]" in r.json()["detail"]


def test_garbage_protobuf_is_a_clean_error(client):
    pytest.importorskip("opentelemetry.proto")
    r = client.post("/v1/traces", content=b"\xff\xff\xff\xff", headers={"content-type": "application/x-protobuf"})
    assert r.status_code == 422


# ----------------------------------------------------------------- the file command
def test_import_otlp_file_reads_one_request_or_json_lines(client, tmp_path):
    one = tmp_path / "one.json"
    one.write_text(json.dumps(request(openinference_trace())), encoding="utf-8")
    done = cli.import_otlp_file(str(one), client, version="v1")
    assert done["spans"] == 3 and done["runs_created"] == 1 and done["agents"] == ["support-bot"]

    lines = tmp_path / "many.jsonl"
    lines.write_text("\n".join(json.dumps(request(openinference_trace(trace=t * 32))) for t in "56") + "\n", encoding="utf-8")
    done = cli.import_otlp_file(str(lines), client, agent="from-file", environment="canary")
    assert done["requests"] == 2 and done["runs_created"] == 2
    assert {r["environment"] for r in runs(client, "from-file")} == {"canary"}




def test_import_otlp_file_reads_gzipped_json(client, tmp_path):
    """gzip-compressed OTLP/JSON files are accepted."""
    payload = request(openinference_trace())
    gz_path = tmp_path / "traces.json.gz"
    with gzip.open(gz_path, "wt", encoding="utf-8") as f:
        json.dump(payload, f)
    done = cli.import_otlp_file(str(gz_path), client)
    assert done["requests"] == 1 and done["runs_created"] == 1
    # also jsonl.gz
    lines = tmp_path / "many.jsonl.gz"
    with gzip.open(lines, "wt", encoding="utf-8") as f:
        for t in "ab":
            f.write(json.dumps(request(openinference_trace(trace=t * 32))) + "\n")
    done = cli.import_otlp_file(str(lines), client, agent="from-gz")
    assert done["requests"] == 2 and done["runs_created"] == 2

def test_import_otlp_file_explains_what_went_wrong(client, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"resourceSpans": 5}', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.import_otlp_file(str(bad), client)
    assert "422" in str(exc.value)
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"a": 1}\nnot json\n', encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        cli.read_otlp_file(str(broken))
    assert "line 2" in str(exc.value)


def test_import_otlp_command_reports_an_unreachable_server(tmp_path, capsys):
    f = tmp_path / "t.json"
    f.write_text(json.dumps(request(openinference_trace())), encoding="utf-8")
    assert cli.main(["import-otlp", str(f), "--url", "http://127.0.0.1:9"]) == 1
    assert "Could not reach" in capsys.readouterr().out


# ----------------------------------------------------------------- the whole path, over real HTTP
def test_plain_opentelemetry_code_reaches_a_canary_verdict(monkeypatch, tmp_path):
    """examples/otel_to_auditor.py: the OTel SDK exports protobuf over HTTP to a live server, and the canary rolls back."""
    pytest.importorskip("opentelemetry.sdk")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
    import socket
    import sys
    import threading
    import time
    from pathlib import Path

    import httpx
    import uvicorn

    from agent_auditor.api.app import create_app

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "examples"))
    import otel_to_auditor

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    app = create_app(database_url="sqlite:///" + str(tmp_path / "otel.db").replace("\\", "/"), api_key="k")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(url + "/healthz", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        report = otel_to_auditor.run(url, "k", runs=30, pause=0)
        assert report["verdict"] == "rollback", report["reasons"]
        assert report["baseline"]["runs"] == 30 and report["candidate"]["runs"] == 30
        assert "gives_an_answer" in report["reasons"][0]
    finally:
        server.should_exit = True
    assert sys.modules.get("otel_to_auditor") is not None
