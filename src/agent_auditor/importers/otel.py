"""Turn OpenTelemetry spans (OTLP) into Agent Auditor runs and steps. Pure: no database, no web framework.

One trace becomes one run. The root span (no parent in the trace) is the run; its descendants that call a model, a tool or a
retriever become steps. Spans that only group other spans (an "agent" or "chain" span with children) are left out.

Where the question, the answer, the model and the token counts are read from, in order of preference:
  OpenInference     openinference.span.kind, input.value, output.value, llm.input_messages.N.*, retrieval.documents.N.*
  OTel GenAI        gen_ai.operation.name, gen_ai.input.messages, gen_ai.output.messages, gen_ai.tool.call.*, gen_ai.usage.*
  OpenLLMetry       traceloop.span.kind, traceloop.entity.input/output, gen_ai.prompt.N.*, gen_ai.completion.N.*
  Langfuse (OTel)   langfuse.observation.type, langfuse.observation.input/output, langfuse.trace.input/output
Anything else still imports, with timing and status, but no question or answer text. Nothing is guessed: a field that is
not found stays empty.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

MAX_TEXT = 20_000
MAX_DOCS = 50
MAX_SPANS = 50_000

_INDEXED = re.compile(r"^(?P<prefix>.+?)\.(?P<i>\d+)\.(?P<rest>.+)$")


class OtlpError(ValueError):
    """The payload is not OTLP trace data."""


# ----------------------------------------------------------------------------- reading OTLP
def _any_value(v: Any) -> Any:
    if not isinstance(v, dict):
        return v
    if "stringValue" in v:
        return v["stringValue"]
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "arrayValue" in v:
        return [_any_value(x) for x in (v["arrayValue"] or {}).get("values", [])]
    if "kvlistValue" in v:
        return {kv.get("key", ""): _any_value(kv.get("value")) for kv in (v["kvlistValue"] or {}).get("values", [])}
    if "bytesValue" in v:
        return v["bytesValue"]
    return None


def attributes(items: Any) -> dict[str, Any]:
    """OTLP attribute list ([{key, value: {stringValue: ...}}]) to a plain dict."""
    out: dict[str, Any] = {}
    for kv in items or []:
        if isinstance(kv, dict) and kv.get("key"):
            out[str(kv["key"])] = _any_value(kv.get("value"))
    return out


def _hex_id(value: Any) -> str:
    """Ids are hex in OTLP/JSON. Protobuf-to-JSON tools write them as base64; accept both."""
    if not isinstance(value, str) or not value:
        return ""
    if re.fullmatch(r"[0-9a-fA-F]+", value) and len(value) in (16, 32):
        return value.lower()
    try:
        return base64.b64decode(value, validate=True).hex()
    except ValueError:
        return value.lower()


def _nanos(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _when(nanos: int | None) -> datetime | None:
    return datetime.fromtimestamp(nanos / 1e9, tz=timezone.utc) if nanos else None


def _status(span: dict[str, Any]) -> tuple[bool, str | None]:
    st = span.get("status") or {}
    code = st.get("code", 0)
    is_error = code == 2 or str(code).upper() in ("STATUS_CODE_ERROR", "ERROR")
    message = (st.get("message") or "").strip() or None
    for ev in span.get("events") or []:
        if ev.get("name") == "exception":
            a = attributes(ev.get("attributes"))
            text = f"{a.get('exception.type', 'Exception')}: {a.get('exception.message', '')}".strip(": ")
            message = text if (is_error or message is None) else message
            is_error = is_error or bool(text)
    return is_error, message[:2000] if message else None


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str
    name: str
    start: int | None
    end: int | None
    error: bool
    error_text: str | None
    attrs: dict[str, Any]
    resource: dict[str, Any]
    kind: int = 0


def parse(payload: Any) -> list[Span]:
    """Read an OTLP/JSON ExportTraceServiceRequest into spans."""
    if not isinstance(payload, dict) or not isinstance(payload.get("resourceSpans"), list):
        raise OtlpError("not OTLP trace data: expected an object with a 'resourceSpans' list")
    spans: list[Span] = []
    for rs in payload["resourceSpans"]:
        resource = attributes(((rs or {}).get("resource") or {}).get("attributes"))
        for scope in (rs or {}).get("scopeSpans") or (rs or {}).get("instrumentationLibrarySpans") or []:
            for sp in (scope or {}).get("spans") or []:
                tid, sid = _hex_id(sp.get("traceId")), _hex_id(sp.get("spanId"))
                if not tid or not sid:
                    continue
                err, text = _status(sp)
                spans.append(Span(
                    trace_id=tid, span_id=sid, parent_id=_hex_id(sp.get("parentSpanId")), name=str(sp.get("name") or "span")[:200],
                    start=_nanos(sp.get("startTimeUnixNano")), end=_nanos(sp.get("endTimeUnixNano")), error=err, error_text=text,
                    attrs=attributes(sp.get("attributes")), resource=resource, kind=int(sp.get("kind") or 0) if str(sp.get("kind") or 0).isdigit() else 0))
                if len(spans) > MAX_SPANS:
                    raise OtlpError(f"more than {MAX_SPANS} spans in one request; send smaller batches")
    return spans


def from_protobuf(body: bytes) -> dict[str, Any]:
    """Decode an OTLP/protobuf request into the same JSON shape. Needs `pip install "agent-auditor[otel]"`."""
    try:
        from google.protobuf.json_format import MessageToDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    except ImportError as exc:
        raise ImportError('OTLP/protobuf needs the OpenTelemetry protos: pip install "agent-auditor[otel]"') from exc
    req = ExportTraceServiceRequest()
    try:
        req.ParseFromString(body)
    except Exception as exc:  # noqa: BLE001 - protobuf raises DecodeError and friends
        raise OtlpError(f"could not decode the protobuf body: {exc}") from exc
    return MessageToDict(req)


# ----------------------------------------------------------------------------- understanding attributes
def _json_or_text(value: Any) -> Any:
    if isinstance(value, str):
        s = value.strip()
        if s[:1] in "[{":
            try:
                return json.loads(s)
            except ValueError:
                pass
        return value[:MAX_TEXT]
    return value


def _indexed(attrs: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    """Collect `prefix.N.rest` attributes into a list of dicts, in index order."""
    rows: dict[int, dict[str, Any]] = {}
    for key, val in attrs.items():
        if not key.startswith(prefix + "."):
            continue
        m = _INDEXED.match(key)
        if m and m.group("prefix") == prefix:
            rows.setdefault(int(m.group("i")), {})[m.group("rest")] = val
    return [rows[i] for i in sorted(rows)]


def _message_rows(attrs: dict[str, Any], *prefixes: str) -> list[dict[str, Any]]:
    out = []
    for prefix in prefixes:
        for row in _indexed(attrs, prefix):
            role = row.get("role") or row.get("message.role")
            content = row.get("content") or row.get("message.content")
            if content is not None:
                out.append({"role": role, "content": content})
        if out:
            break
    return out


def message_text(messages: Any, role: str) -> str | None:
    """The text of the last message from `role` ('user' or 'assistant') in a list of chat messages."""
    if not isinstance(messages, list):
        return None
    for m in reversed(messages):
        if not isinstance(m, dict) or (m.get("role") or "").lower() != role:
            continue
        content = m.get("content")
        if content is None and isinstance(m.get("parts"), list):
            content = m["parts"]
        if isinstance(content, str):
            return content[:MAX_TEXT]
        if isinstance(content, list):
            texts = [p.get("text") or p.get("content") for p in content if isinstance(p, dict)]
            texts += [p for p in content if isinstance(p, str)]
            joined = " ".join(t for t in texts if isinstance(t, str))
            if joined.strip():
                return joined[:MAX_TEXT]
    return None


_INPUT_KEYS = ("input.value", "langfuse.observation.input", "langfuse.trace.input", "traceloop.entity.input", "gen_ai.tool.call.arguments",
               "tool.parameters", "gen_ai.input.messages")
_OUTPUT_KEYS = ("output.value", "langfuse.observation.output", "langfuse.trace.output", "traceloop.entity.output", "gen_ai.tool.call.result",
                "gen_ai.output.messages")


def _io(attrs: dict[str, Any], which: str) -> Any:
    keys = _INPUT_KEYS if which == "input" else _OUTPUT_KEYS
    # Chat messages are cleaner than the raw request or response JSON that instrumentors also put in input.value / output.value.
    structured = _message_rows(attrs, "llm.input_messages") if which == "input" else _message_rows(attrs, "llm.output_messages")
    if structured:
        return structured
    for k in keys:
        if attrs.get(k) not in (None, ""):
            return _json_or_text(attrs[k])
    if which == "input":
        rows = _message_rows(attrs, "llm.input_messages", "gen_ai.prompt")
        if rows:
            return rows
        if attrs.get("gen_ai.prompt") not in (None, ""):
            return _json_or_text(attrs["gen_ai.prompt"])
    else:
        rows = _message_rows(attrs, "llm.output_messages", "gen_ai.completion")
        if rows:
            return rows
        if attrs.get("gen_ai.completion") not in (None, ""):
            return _json_or_text(attrs["gen_ai.completion"])
    return None


_OPENINFERENCE = {"LLM": "llm", "TOOL": "tool", "RETRIEVER": "retrieval", "EMBEDDING": "other", "RERANKER": "other",
                  "CHAIN": "other", "AGENT": "other", "GUARDRAIL": "other", "EVALUATOR": "other"}
_GENAI_OPS = {"chat": "llm", "text_completion": "llm", "generate_content": "llm", "completion": "llm",
              "execute_tool": "tool", "retrieval": "retrieval", "embeddings": "other", "invoke_agent": "other",
              "create_agent": "other", "invoke_workflow": "other"}
_LANGFUSE = {"generation": "llm", "tool": "tool", "retriever": "retrieval", "span": "other", "agent": "other", "chain": "other",
             "embedding": "other", "guardrail": "other", "evaluator": "other", "event": "other"}
_TRACELOOP = {"tool": "tool", "workflow": "other", "task": "other", "agent": "other"}


def classify(attrs: dict[str, Any]) -> str:
    """llm | tool | retrieval | other, from the first convention that names the span's job."""
    oi = str(attrs.get("openinference.span.kind") or "").upper()
    if oi in _OPENINFERENCE:
        return _OPENINFERENCE[oi]
    op = str(attrs.get("gen_ai.operation.name") or "").lower()
    if op in _GENAI_OPS:
        return _GENAI_OPS[op]
    lf = str(attrs.get("langfuse.observation.type") or "").lower()
    if lf in _LANGFUSE:
        return _LANGFUSE[lf]
    tl = str(attrs.get("traceloop.span.kind") or "").lower()
    if tl in _TRACELOOP:
        return _TRACELOOP[tl]
    if any(k in attrs for k in ("gen_ai.request.model", "llm.model_name", "llm.request.type", "gen_ai.system", "gen_ai.provider.name")):
        return "llm"
    if "tool.name" in attrs or "gen_ai.tool.name" in attrs:
        return "tool"
    return "other"


def _first(attrs: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if attrs.get(k) not in (None, ""):
            return attrs[k]
    return None


def _num(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _step_attributes(sp: Span, kind: str) -> dict[str, Any]:
    a = sp.attrs
    out: dict[str, Any] = {"otel.span_id": sp.span_id, "otel.parent_span_id": sp.parent_id, "otel.span_name": sp.name}
    model = _first(a, "gen_ai.request.model", "llm.model_name", "gen_ai.response.model")
    if model:
        out["model"] = str(model)
    provider = _first(a, "gen_ai.system", "gen_ai.provider.name", "llm.provider", "llm.system")
    if provider:
        out["provider"] = str(provider)
    tin = _num(_first(a, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.token_count.prompt"))
    tout = _num(_first(a, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "llm.token_count.completion"))
    if tin is not None:
        out["input_tokens"] = tin
    if tout is not None:
        out["output_tokens"] = tout
    if kind == "retrieval":
        docs = _indexed(a, "retrieval.documents")
        texts = [str(d.get("document.content"))[:MAX_TEXT] for d in docs[:MAX_DOCS] if d.get("document.content") is not None]
        scores = [float(d["document.score"]) for d in docs[:MAX_DOCS] if _num(d.get("document.score")) is not None]
        if docs:
            out["num_docs"] = len(docs)
        if scores:
            out["scores"] = scores
        if texts:
            out["_documents"] = texts
    return out


# ----------------------------------------------------------------------------- spans to a run
@dataclass
class StepDraft:
    span_id: str
    kind: str
    name: str
    input: Any
    output: Any
    status: str
    error: str | None
    started_at: datetime | None
    ended_at: datetime | None
    attributes: dict[str, Any]


@dataclass
class RunDraft:
    trace_id: str
    agent: str
    environment: str
    version: str | None
    has_root: bool
    started_at: datetime | None
    ended_at: datetime | None
    input: Any = None
    output: Any = None
    status: str = "ok"
    error: str | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    steps: list[StepDraft] = field(default_factory=list)


def map_environment(value: Any) -> str:
    v = str(value or "").strip().lower()
    if v == "canary":
        return "canary"
    if v in ("test", "testing", "staging", "stage", "dev", "development", "ci", "local"):
        return "test"
    return "production"


_LC_ROLES = {"human": "user", "user": "user", "ai": "assistant", "assistant": "assistant", "system": "system", "tool": "tool"}


def _chat_messages(items: list[Any]) -> list[dict[str, Any]]:
    """Plain {role, content} messages from OpenAI-style or LangChain-serialised ones."""
    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        data = m.get("data") if isinstance(m.get("data"), dict) else m
        raw = str(m.get("role") or m.get("type") or data.get("type") or "").lower()
        content = data.get("content")
        if raw in _LC_ROLES and content not in (None, "", []):
            out.append({"role": _LC_ROLES[raw], "content": content})
    return out


_QUESTION_KEYS = ("task", "question", "query", "input", "prompt", "message", "text", "user_input")
_ANSWER_KEYS = ("output", "answer", "result", "response", "text", "content")


def _text_of(value: Any, role: str) -> Any:
    """Prefer plain text for a run's question or answer, so scorers can read it."""
    if isinstance(value, list):
        return message_text(value, role) or value
    if isinstance(value, dict) and isinstance(value.get("messages"), list):
        # A graph's state, e.g. LangGraph's {"messages": [...]}, where messages may be in LangChain's {"type": "ai", "data": {...}} form.
        text = message_text(_chat_messages(value["messages"]), role)
        if text:
            return text
    if isinstance(value, dict):
        # Agent frameworks record the call's arguments, e.g. {"task": "...", "stream": false}: take the one field that is the question.
        keys = _QUESTION_KEYS if role == "user" else _ANSWER_KEYS
        for k in keys:
            if isinstance(value.get(k), str) and value[k].strip():
                return value[k][:MAX_TEXT]
    return value


def build_runs(spans: list[Span], *, agent: str | None = None, environment: str | None = None,
               version: str | None = None) -> list[RunDraft]:
    """Group spans by trace and build one run draft per trace (and per service, if a trace crosses services)."""
    groups: dict[tuple[str, str], list[Span]] = {}
    for sp in spans:
        name = agent or str(sp.resource.get("service.name") or "otel-agent")
        groups.setdefault((name, sp.trace_id), []).append(sp)
    return [_one_run(name, tid, group, environment, version) for (name, tid), group in groups.items()]


def _one_run(agent: str, trace_id: str, spans: list[Span], environment: str | None, version: str | None) -> RunDraft:
    roots = sorted((s for s in spans if not s.parent_id), key=lambda s: s.start or 0)
    root = roots[0] if roots else None
    has_children = {s.parent_id for s in spans if s.parent_id}
    res = (root or spans[0]).resource
    env = environment or map_environment(res.get("deployment.environment.name") or res.get("deployment.environment"))
    ver = version or (str(res["service.version"])[:100] if res.get("service.version") else None)

    steps: list[StepDraft] = []
    for sp in sorted(spans, key=lambda s: (s.start or 0)):
        if root is not None and sp.span_id == root.span_id:
            continue
        kind = classify(sp.attrs)
        if kind == "other" and sp.span_id in has_children:
            continue  # a span that only groups other spans
        a = _step_attributes(sp, kind)
        docs = a.pop("_documents", None)
        out = docs if docs is not None else _io(sp.attrs, "output")
        steps.append(StepDraft(sp.span_id, kind, sp.name, _io(sp.attrs, "input"), out, "error" if sp.error else "ok", sp.error_text,
                               _when(sp.start), _when(sp.end), a))

    draft = RunDraft(trace_id=trace_id, agent=agent, environment=env, version=ver, has_root=root is not None,
                     started_at=_when(min((s.start for s in spans if s.start), default=None)),
                     ended_at=_when(max((s.end for s in spans if s.end), default=None)), steps=steps)
    draft.tags = {"source": "otlp", "trace_id": trace_id}
    if root is not None:
        a = root.attrs
        draft.started_at, draft.ended_at = _when(root.start), _when(root.end)
        draft.input = _text_of(_io(a, "input"), "user")
        draft.output = _text_of(_io(a, "output"), "assistant")
        if root.error:
            draft.status, draft.error = "error", root.error_text or "the root span ended with an error"
        draft.tags["root"] = root.name
        if res.get("service.name"):
            draft.tags["service"] = str(res["service.name"])
    if root is not None and not any(s.parent_id == root.span_id for s in spans):
        # The root is the only span (a bare model or tool call): it is also the run's one step, so its timing and model count.
        kind = classify(root.attrs)
        if kind != "other":
            a = _step_attributes(root, kind)
            docs = a.pop("_documents", None)
            out = docs if docs is not None else _io(root.attrs, "output")
            draft.steps.append(StepDraft(root.span_id, kind, root.name, _io(root.attrs, "input"), out, "error" if root.error else "ok",
                                         root.error_text, _when(root.start), _when(root.end), a))
            steps = draft.steps
    fill_from_steps(draft)
    models = [s.attributes["model"] for s in steps if "model" in s.attributes]
    if models:
        draft.tags["model"] = models[0]
    return draft


def fill_from_steps(draft: RunDraft) -> None:
    """A run with no question or answer of its own takes them from its first and last model call."""
    llm = [s for s in draft.steps if s.kind == "llm"]
    if draft.input is None and llm:
        draft.input = _text_of(llm[0].input, "user")
    if draft.output is None and llm:
        ok = [s for s in llm if s.status == "ok" and s.output is not None]
        if ok:
            draft.output = _text_of(ok[-1].output, "assistant")
