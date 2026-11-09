"""Framework-neutral mapping from callback-style events to Auditor runs and steps.

Adapters (LangGraph/LangChain today, others later) translate their own event objects into the
simple calls below. Keeping this layer free of framework imports means it can be tested anywhere.

Model: the outermost event with no tracked parent starts a Run. Child events become Steps on that
Run. Chain events are only recorded as steps when they are LangGraph nodes (their name equals the
`langgraph_node` metadata); internal chain plumbing is skipped but still lets children find the root.
"""
from __future__ import annotations

import threading
from typing import Any, Hashable

from .tracer import Auditor, Run, Step


class CallbackMapper:
    def __init__(
        self,
        auditor: Auditor,
        *,
        node_kinds: dict[str, str] | None = None,
        include_nodes: bool = True,
        tags: dict[str, Any] | None = None,
    ) -> None:
        self._auditor = auditor
        self._node_kinds = dict(node_kinds or {})
        self._include_nodes = include_nodes
        self._tags = dict(tags or {})
        self._lock = threading.Lock()
        self._roots: dict[Hashable, Run] = {}
        self._root_of: dict[Hashable, Hashable] = {}
        self._members: dict[Hashable, list[Hashable]] = {}
        self._steps: dict[Hashable, Step] = {}

    def _is_node(self, name: str, metadata: dict[str, Any] | None) -> bool:
        return bool(self._include_nodes and metadata and metadata.get("langgraph_node") == name)

    def start(
        self,
        *,
        kind: str,
        run_id: Hashable,
        parent_run_id: Hashable | None,
        name: str,
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            root_id = self._root_of.get(parent_run_id) if parent_run_id is not None else None
            is_root = root_id is None
            if is_root:
                run = self._auditor.run(input=input, tags={**self._tags, "root": name})
                self._roots[run_id] = run
                self._root_of[run_id] = run_id
                self._members[run_id] = [run_id]
                root_id = run_id
            else:
                self._root_of[run_id] = root_id
                self._members[root_id].append(run_id)
            run = self._roots[root_id]

            if kind == "chain":
                if is_root or not self._is_node(name, metadata):
                    return
                step_kind = self._node_kinds.get(name, "other")
            else:
                step_kind = kind
            self._steps[run_id] = run.step(name, kind=step_kind, input=input, attributes=attributes)

    def end(self, *, run_id: Hashable, output: Any = None, attributes: dict[str, Any] | None = None) -> None:
        with self._lock:
            step = self._steps.pop(run_id, None)
            root = self._roots.get(run_id)
        if step is not None:
            if output is not None:
                step.output = output
            if attributes:
                step.attributes.update(attributes)
            step.end()
        if root is not None:
            root.output = output
            self._finish_root(run_id)

    def error(self, *, run_id: Hashable, error: Any) -> None:
        with self._lock:
            step = self._steps.pop(run_id, None)
            root = self._roots.get(run_id)
        if step is not None:
            step.fail(error)
            step.end()
        if root is not None:
            root.fail(error)
            self._finish_root(run_id)

    def _finish_root(self, root_id: Hashable) -> None:
        with self._lock:
            run = self._roots.pop(root_id, None)
            for member in self._members.pop(root_id, []):
                self._root_of.pop(member, None)
                self._steps.pop(member, None)
        if run is not None:
            run.finish()
