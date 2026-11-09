from __future__ import annotations

import hashlib
import json
from typing import Any


def content_hash(prompt: str | None, config: dict[str, Any]) -> str:
    """Stable identity for an agent version: same prompt + config => same hash."""
    payload = json.dumps(
        {"prompt": prompt, "config": config}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
