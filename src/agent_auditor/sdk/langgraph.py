"""LangGraph / LangChain adapter.

    from agent_auditor.sdk import Auditor
    from agent_auditor.sdk.langgraph import AuditorCallbackHandler

    auditor = Auditor(agent="support-bot")
    handler = AuditorCallbackHandler(auditor, node_kinds={"retrieve": "retrieval"})
    graph.invoke(inputs, config={"callbacks": [handler]})

Each top-level invocation becomes one Run. LangGraph nodes, LLM calls, tool calls and retriever
calls become Steps. Requires: pip install "agent-auditor[langgraph]"
"""
from __future__ import annotations

import functools
import logging
from typing import Any

try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError as exc:  # pragma: no cover
    raise ImportError('LangChain support needs: pip install "agent-auditor[langgraph]"') from exc

from ._mapper import CallbackMapper
from .tracer import Auditor

log = logging.getLogger("agent_auditor")

_SCORE_KEYS = ("score", "relevance_score", "similarity", "similarity_score")


def _safe(fn):
    """A tracing bug must never break the agent being traced."""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:
            log.exception("agent-auditor: callback %s failed", fn.__name__)

    return wrapper


def _name(serialized: dict | None, kwargs: dict, default: str) -> str:
    if kwargs.get("name"):
        return str(kwargs["name"])
    if serialized:
        if serialized.get("name"):
            return str(serialized["name"])
        ident = serialized.get("id")
        if isinstance(ident, list) and ident:
            return str(ident[-1])
    return default


def _messages(batches: Any) -> Any:
    out = []
    for batch in batches or []:
        out.append(
            [{"role": getattr(m, "type", type(m).__name__), "content": getattr(m, "content", str(m))} for m in batch]
        )
    return out[0] if len(out) == 1 else out


def _model_name(kwargs: dict, metadata: dict | None) -> str | None:
    params = kwargs.get("invocation_params") or {}
    return params.get("model") or params.get("model_name") or (metadata or {}).get("ls_model_name")


class AuditorCallbackHandler(BaseCallbackHandler):
    """Pass in `config={"callbacks": [handler]}` when invoking a graph, chain, model or tool."""

    raise_error = False
    run_inline = True

    def __init__(
        self,
        auditor: Auditor,
        *,
        node_kinds: dict[str, str] | None = None,
        include_nodes: bool = True,
        tags: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._mapper = CallbackMapper(
            auditor, node_kinds=node_kinds, include_nodes=include_nodes, tags={"framework": "langchain", **(tags or {})}
        )

    # -- chains / graph nodes ---------------------------------------------
    @_safe
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        self._mapper.start(
            kind="chain", run_id=run_id, parent_run_id=parent_run_id,
            name=_name(serialized, kwargs, "chain"), input=inputs, metadata=metadata,
        )

    @_safe
    def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
        self._mapper.end(run_id=run_id, output=outputs)

    @_safe
    def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._mapper.error(run_id=run_id, error=error)

    # -- LLMs -------------------------------------------------------------
    @_safe
    def on_llm_start(self, serialized, prompts, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        self._mapper.start(
            kind="llm", run_id=run_id, parent_run_id=parent_run_id, name=_name(serialized, kwargs, "llm"),
            input=prompts, metadata=metadata, attributes={"model": _model_name(kwargs, metadata)},
        )

    @_safe
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        self._mapper.start(
            kind="llm", run_id=run_id, parent_run_id=parent_run_id, name=_name(serialized, kwargs, "chat_model"),
            input=_messages(messages), metadata=metadata, attributes={"model": _model_name(kwargs, metadata)},
        )

    @_safe
    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kwargs):
        text, usage = None, None
        try:
            generation = response.generations[0][0]
            text = generation.text
            usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
        except Exception:
            pass
        usage = usage or (getattr(response, "llm_output", None) or {}).get("token_usage")
        self._mapper.end(run_id=run_id, output=text, attributes={"token_usage": usage} if usage else None)

    @_safe
    def on_llm_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._mapper.error(run_id=run_id, error=error)

    # -- tools ------------------------------------------------------------
    @_safe
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, tags=None, metadata=None, inputs=None, **kwargs):
        self._mapper.start(
            kind="tool", run_id=run_id, parent_run_id=parent_run_id, name=_name(serialized, kwargs, "tool"),
            input=inputs if inputs is not None else input_str, metadata=metadata,
        )

    @_safe
    def on_tool_end(self, output, *, run_id, parent_run_id=None, **kwargs):
        self._mapper.end(run_id=run_id, output=getattr(output, "content", output))

    @_safe
    def on_tool_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._mapper.error(run_id=run_id, error=error)

    # -- retrievers -------------------------------------------------------
    @_safe
    def on_retriever_start(self, serialized, query, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        self._mapper.start(
            kind="retrieval", run_id=run_id, parent_run_id=parent_run_id,
            name=_name(serialized, kwargs, "retriever"), input=query, metadata=metadata,
        )

    @_safe
    def on_retriever_end(self, documents, *, run_id, parent_run_id=None, **kwargs):
        docs, scores = [], []
        for d in documents or []:
            meta = dict(getattr(d, "metadata", None) or {})
            docs.append({"content": str(getattr(d, "page_content", d))[:500], "metadata": meta})
            for key in _SCORE_KEYS:
                if isinstance(meta.get(key), (int, float)):
                    scores.append(float(meta[key]))
                    break
        attrs: dict[str, Any] = {"num_docs": len(docs)}
        if scores:
            attrs["scores"] = scores
        self._mapper.end(run_id=run_id, output=docs, attributes=attrs)

    @_safe
    def on_retriever_error(self, error, *, run_id, parent_run_id=None, **kwargs):
        self._mapper.error(run_id=run_id, error=error)
