"""Connection between the chat and Agent Auditor: registers versions, retries until the server is up, forwards thumbs up/down."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

import httpx

from agent import SYSTEM
from capabilities import EXTRAS, FEATURES
from providers import Provider


class Tracing:
    def __init__(self, url: str | None, agent: str, api_key: str | None, tool_names: list[str], default: Provider, auditor: Any = None) -> None:
        self.url = (url or "").rstrip("/")
        self.agent_name = agent
        self.api_key = api_key
        self.tool_names = sorted(tool_names)
        self.default = default
        self.state = "off" if not self.url else "connecting"
        self.error = ""
        self.labels: dict[str, str] = {}
        self._lock = threading.Lock()
        self.auditor = auditor
        if self.url and self.auditor is None:
            from agent_auditor.sdk import Auditor
            self.auditor = Auditor(self.url, agent=agent, api_key=api_key)

    # ---- versions
    def _config(self, p: Provider) -> dict[str, Any]:
        return {"provider": p.name, "model": p.model, "tools": self.tool_names, "features": FEATURES, "extras": EXTRAS}

    def label(self, p: Provider) -> str:
        return "chat-" + hashlib.sha256(json.dumps([SYSTEM, self._config(p)], sort_keys=True).encode()).hexdigest()[:8]

    def connect(self) -> bool:
        """Register the default model's version. Returns True when the Auditor accepted it."""
        if not self.auditor:
            return False
        from agent_auditor.sdk import AuditorError
        try:
            self.auditor.register_version(self.label(self.default), prompt=SYSTEM, config=self._config(self.default))
        except AuditorError as exc:
            self.state, self.error = "error", str(exc)
            return False
        with self._lock:
            self.labels[f"{self.default.name}:{self.default.model}"] = self.label(self.default)
        self.state, self.error = "on", ""
        return True

    def version_for(self, p: Provider) -> str | None:
        """The version label for any model the person picked. Other models are registered but not made active."""
        if self.state != "on":
            return None
        key = f"{p.name}:{p.model}"
        with self._lock:
            known = self.labels.get(key)
        if known:
            return known
        from agent_auditor.sdk import AuditorError
        label = self.label(p)
        try:
            self.auditor.register_version(label, prompt=SYSTEM, config=self._config(p), activate=False, use=False)
        except AuditorError:
            return None
        with self._lock:
            self.labels[key] = label
        return label

    def start_background(self) -> None:
        """Keep trying until the Auditor is reachable, so starting the chat before the server does not switch tracing off for good."""
        if not self.auditor or self.state == "on":
            return

        def loop() -> None:
            wait = 2.0
            while self.state != "on":
                if self.connect():
                    print(f"Tracing ON: every chat turn goes to {self.url} as agent '{self.agent_name}'.", flush=True)
                    return
                time.sleep(wait)
                wait = min(wait * 1.5, 20)
        threading.Thread(target=loop, name="tracing-connect", daemon=True).start()

    # ---- feedback
    def send_feedback(self, turn_id: str, rating: str, comment: str | None) -> tuple[bool, str]:
        if not self.url:
            return False, "tracing is off"
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        last = ""
        for _ in range(8):  # the run is sent in the background, so it may not have arrived yet
            try:
                r = httpx.post(f"{self.url}/v1/feedback", headers=headers, timeout=5,
                               json={"agent": self.agent_name, "turn_id": turn_id, "rating": rating, "comment": comment})
            except httpx.HTTPError as exc:
                last = f"could not reach the Auditor: {exc}"
                time.sleep(1.5)
                continue
            if r.status_code == 200:
                return True, "sent"
            last = f"{r.status_code} {r.text[:120]}"
            if r.status_code != 404:
                break
            time.sleep(1.5)
        return False, last

    def status(self) -> dict[str, Any]:
        stats = self.auditor.stats if self.auditor else {}
        return {"state": self.state, "url": self.url or None, "agent": self.agent_name, "error": self.error,
                "sent": stats.get("sent", 0), "failed": stats.get("failed", 0), "dropped": stats.get("dropped", 0),
                "dashboard": f"{self.url}/dashboard" if self.url else None}
