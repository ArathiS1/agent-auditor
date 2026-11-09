"""Turn arbitrary Python objects into bounded, JSON-safe data.

Agents pass around prompts, documents, pydantic models, dataclasses and other objects. The SDK
must never crash the host agent or ship megabytes of text, so everything is converted here with
hard limits on string length, collection size and nesting depth.
"""
from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime
from enum import Enum
from typing import Any
from uuid import UUID


def _truncate(text: str, max_str: int) -> str:
    if len(text) <= max_str:
        return text
    return text[:max_str] + f"...[truncated {len(text) - max_str} chars]"


def to_jsonable(obj: Any, *, max_str: int = 4000, max_items: int = 50, max_depth: int = 6) -> Any:
    return _conv(obj, max_str, max_items, max_depth, 0)


def _conv(obj: Any, ms: int, mi: int, md: int, depth: int) -> Any:
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, Enum):
        return _conv(obj.value, ms, mi, md, depth + 1)
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, str):
        return _truncate(obj, ms)
    if isinstance(obj, (bytes, bytearray)):
        return f"<{len(obj)} bytes>"
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    if depth >= md:
        return _truncate(repr(obj), ms)

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for i, (key, value) in enumerate(obj.items()):
            if i >= mi:
                out["..."] = f"+{len(obj) - mi} more keys"
                break
            out[_truncate(str(key), 200)] = _conv(value, ms, mi, md, depth + 1)
        return out
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = list(obj)
        result = [_conv(v, ms, mi, md, depth + 1) for v in items[:mi]]
        if len(items) > mi:
            result.append(f"...(+{len(items) - mi} more)")
        return result
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        fields = {f.name: getattr(obj, f.name, None) for f in dataclasses.fields(obj)}
        return _conv(fields, ms, mi, md, depth)
    for method in ("model_dump", "dict"):
        fn = getattr(obj, method, None)
        if callable(fn):
            try:
                return _conv(fn(), ms, mi, md, depth + 1)
            except Exception:
                continue
    return _truncate(repr(obj), ms)
