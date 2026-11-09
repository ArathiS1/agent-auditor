"""A ChatGPT-style chat app that runs on your own computer.

  python chatbot/server.py            then open http://127.0.0.1:8001

Needs an API key: ANTHROPIC_API_KEY (Claude), or a free GEMINI_API_KEY / GROQ_API_KEY, or run Ollama locally (see README). Set AUDITOR_URL (and AUDITOR_API_KEY) to send every chat turn to Agent Auditor.
No web framework is used: this is the Python standard library plus httpx.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

from envfile import load_env, problems  # noqa: E402

_ENV_FILES = load_env(HERE)  # settings from .env, before anything reads the environment

from agent import ChatAgent, _turn_start_indexes, to_display  # noqa: E402
from capabilities import EXTRAS, FEATURES  # noqa: E402
import router  # noqa: E402
from providers import available_models, display_name, openrouter_free_models, provider_for  # noqa: E402
from tracing import Tracing  # noqa: E402
from files import MAX_FILE_BYTES, FileProblem, extract  # noqa: E402
from store import Store  # noqa: E402
from tools import Toolbox, _load_notes_index  # noqa: E402

MAX_MESSAGE = 8000
MAX_ATTACHMENTS = 6
DEFAULT_FILE_QUESTION = "Please read the attached file(s) and give me a short summary, then ask what I would like to know."
INDEX_HTML = (HERE / "static" / "index.html").read_text(encoding="utf-8")


class App:
    def __init__(self, agent: ChatAgent, store: Store, port: int, tracing: Tracing | None = None) -> None:
        self.agent, self.store, self.port = agent, store, port
        self.tracing = tracing
        self.busy: dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "auditor-chat"

        def log_message(self, *args: Any) -> None:
            pass

        # ---- helpers
        def _headers(self, code: int, kind: str, length: int | None = None, extra: dict[str, str] | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode()
            self._headers(code, "application/json", len(body))
            self.wfile.write(body)

        def _safe(self) -> bool:
            """Only answer requests meant for this local app (blocks DNS-rebinding and cross-site posts)."""
            if self.headers.get("Host", "") not in app.allowed_hosts():
                self._json(403, {"error": "unexpected host"})
                return False
            origin = self.headers.get("Origin")
            if self.command != "GET" and origin and urlparse(origin).netloc != self.headers.get("Host"):
                self._json(403, {"error": "cross-site request refused"})
                return False
            return True

        def _body(self, limit: int = 100_000) -> dict[str, Any] | None:
            try:
                n = int(self.headers.get("Content-Length", 0))
                if n > limit or "json" not in self.headers.get("Content-Type", ""):
                    raise ValueError
                data = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError
                return data
            except ValueError:
                self._json(400, {"error": "send a JSON object"})
                return None

        def _provider(self, data: dict[str, Any]) -> Any:
            """The model picked in the window (or the default). Returns None after sending an error."""
            spec = str(data.get("model") or "")
            if not spec:
                return app.agent.provider
            p = provider_for(spec)
            if p is None:
                self._json(400, {"error": "unknown model"})
                return None
            if not p.ready:
                self._json(400, {"error": f"{display_name(p)} needs {p.key_env} in your .env file, then restart the chat."})
                return None
            return p

        # ---- routes
        def do_GET(self) -> None:  # noqa: N802
            if not self._safe():
                return
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                nonce = secrets.token_urlsafe(16)
                body = INDEX_HTML.replace("__NONCE__", nonce).encode()
                csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
                       "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
                self._headers(200, "text/html; charset=utf-8", len(body), {"Content-Security-Policy": csp})
                self.wfile.write(body)
            elif path == "/api/status":
                self._json(200, {"has_key": app.agent.has_key, "model": app.agent.model, "provider": app.agent.provider.name,
                                 "provider_name": display_name(app.agent.provider), "key_env": app.agent.provider.key_env,
                                 "tracing": app.agent.auditor is not None, "trace": app.tracing.status() if app.tracing else {"state": "off"},
                                 "tools": [t["name"] for t in app.agent.tools.defs], "features": FEATURES + EXTRAS,
                                 "memory": len(app.store.memories())})
            elif path == "/api/dashboard-link":
                t = app.tracing
                if not t or not t.url:
                    return self._json(404, {"error": "tracing is off"})
                # the key travels in the URL fragment, which the browser never sends to any server
                self._json(200, {"url": t.url + "/dashboard" + (f"#key={t.api_key}" if t.api_key else "")})
            elif path == "/api/models":
                self._json(200, {"models": _alive(_reachable(_with_discovered(available_models(include_unready=True)))), "current": f"{app.agent.provider.name}:{app.agent.model}"})
            elif path == "/api/health":
                self._json(200, {"auto_route": router.enabled(), "slow_after_seconds": router.SLOW_SECONDS, "models": router.snapshot()})
            elif path == "/api/memory":
                self._json(200, {"items": app.store.memories()})
            elif path == "/api/search":
                self._json(200, {"results": app.store.search(parse_qs(urlparse(self.path).query).get("q", [""])[0])})
            elif path == "/api/conversations":
                self._json(200, {"conversations": app.store.list()})
            elif path.startswith("/api/conversations/"):
                cid = path.rsplit("/", 1)[1]
                if not app.store.exists(cid):
                    return self._json(404, {"error": "conversation not found"})
                shown = to_display(app.store.messages(cid))
                turns, thumbs = app.store.turns(cid), app.store.feedback_for(cid)
                answers = [m for m in shown if m["role"] == "assistant"]
                for m, t in zip(answers, turns[-len(answers):] if answers else []):
                    m.update(turn_id=t["turn_id"], model=t["model"], provider=t["provider"], rating=thumbs.get(t["turn_id"]))
                self._json(200, {"id": cid, "messages": shown})
            else:
                self._json(404, {"error": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._safe():
                return
            path = urlparse(self.path).path
            if path.startswith("/api/conversations/"):
                app.store.delete(path.rsplit("/", 1)[1])
                return self._json(200, {"ok": True})
            if path.startswith("/api/memory/"):
                return self._json(200, {"removed": app.store.forget_memory(path.rsplit("/", 1)[1])})
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._safe():
                return
            path = urlparse(self.path).path
            data = self._body(int(MAX_FILE_BYTES * 1.4) if path == "/api/upload" else 100_000)
            if data is None:
                return
            if path == "/api/conversations":
                return self._json(201, {"id": app.store.create()})
            if path == "/api/upload":
                return self._upload(data)
            if path == "/api/stop":
                with app.lock:
                    running = app.busy.get(str(data.get("conversation_id", "")))
                if running is not None:
                    running.set()
                return self._json(200, {"ok": True})
            if path == "/api/feedback":
                return self._feedback(data)
            if path == "/api/followups":
                return self._followups(data)
            if path == "/api/compare":
                return self._compare(data)
            if path == "/api/vote":
                return self._vote(data)
            if path == "/api/memory":
                try:
                    return self._json(201, {"id": app.store.add_memory(str(data.get("text", "")))})
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
            if path == "/api/retry-tracing":
                ok = bool(app.tracing and app.tracing.connect())
                return self._json(200, {"ok": ok, "trace": app.tracing.status() if app.tracing else {"state": "off"}})
            if path != "/api/chat":
                return self._json(404, {"error": "not found"})
            cid, text = str(data.get("conversation_id", "")), str(data.get("message", "")).strip()
            if not app.store.exists(cid):
                return self._json(404, {"error": "conversation not found"})
            attachments = [str(x) for x in (data.get("attachments") or [])][:MAX_ATTACHMENTS]
            attachments = [x for x in attachments if app.store.document(cid, x)]
            research = data.get("mode") == "research"
            provider = self._provider(data)
            if provider is None:
                return
            redo = bool(data.get("redo"))
            if redo or data.get("edit_index") is not None:
                with app.lock:
                    if cid in app.busy:
                        return self._json(409, {"error": "this chat is still answering"})
                msgs = app.store.messages(cid)
                starts = _turn_start_indexes(msgs)
                if not starts:
                    return self._json(400, {"error": "nothing to redo"})
                if redo:
                    n = len(starts) - 1
                    app.store.rewind(cid, starts[n] + 1)
                    shown = to_display(app.store.messages(cid))
                    text = next((m["text"] for m in reversed(shown) if m["role"] == "user"), "")
                else:
                    try:
                        n = int(data["edit_index"])
                        app.store.rewind(cid, starts[n])
                    except (ValueError, IndexError, TypeError):
                        return self._json(400, {"error": "that message cannot be edited"})
                app.store.keep_turns(cid, n)
                attachments = []
            if not text and attachments:
                text = DEFAULT_FILE_QUESTION
            if not text or len(text) > MAX_MESSAGE:
                return self._json(400, {"error": f"write a message of 1 to {MAX_MESSAGE} characters"})
            with app.lock:
                running = app.busy.get(cid)
            if running is not None:  # an earlier answer is still winding down (for example after Stop): ask it to finish
                running.set()
                for _ in range(40):
                    time.sleep(0.1)
                    with app.lock:
                        if cid not in app.busy:
                            break
            with app.lock:
                if cid in app.busy:
                    return self._json(409, {"error": "this chat is still answering"})
                cancel = app.busy[cid] = threading.Event()
            self._headers(200, "text/event-stream", None, {"X-Accel-Buffering": "no"})
            events = _with_fallback(app.agent, cid, text, cancel, attachments, research, provider, redo)
            try:
                for ev in events:
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # the browser went away; closing the generator cancels the turn cleanly
            finally:
                events.close()
                with app.lock:
                    app.busy.pop(cid, None)

        def _feedback(self, data: dict[str, Any]) -> None:
            turn_id, rating = str(data.get("turn_id", ""))[:40], str(data.get("rating", ""))
            if not turn_id or rating not in ("up", "down"):
                return self._json(400, {"error": "send turn_id and rating (up or down)"})
            comment = str(data.get("comment") or "")[:1000] or None
            app.store.save_feedback(turn_id, str(data.get("conversation_id", "")), rating, comment)
            if app.tracing and app.tracing.state != "off":
                def send() -> None:
                    ok, _ = app.tracing.send_feedback(turn_id, rating, comment)
                    if ok:
                        app.store.mark_feedback_sent(turn_id)
                threading.Thread(target=send, daemon=True).start()
            self._json(200, {"ok": True, "sent_to_auditor": bool(app.tracing and app.tracing.state != "off")})

        def _followups(self, data: dict[str, Any]) -> None:
            cid = str(data.get("conversation_id", ""))
            if not app.store.exists(cid):
                return self._json(404, {"error": "conversation not found"})
            provider = self._provider(data)
            if provider is None:
                return
            if os.environ.get("CHAT_FOLLOWUPS", "").strip() not in ("1", "true", "yes", "on"):
                return self._json(200, {"suggestions": []})  # off by default: each one costs an extra request on a free quota
            try:
                self._json(200, {"suggestions": app.agent.suggest_followups(cid, provider)})
            except Exception:  # suggestions are a nicety: never an error on screen
                self._json(200, {"suggestions": []})

        def _compare(self, data: dict[str, Any]) -> None:
            """Ask the same question of two to four models at once. Each answer is traced as its own run."""
            text = str(data.get("message", "")).strip()
            specs = [str(x) for x in (data.get("models") or [])][:4]
            if not text or len(text) > 2000 or not 2 <= len(specs) <= 4:
                return self._json(400, {"error": "write a question (up to 2000 characters) and pick 2 to 4 models"})
            providers = []
            for spec in specs:
                p = provider_for(spec)
                if p is None or not p.ready:
                    return self._json(400, {"error": f"{spec} is not available (missing key?)"})
                providers.append(p)
            results: list[dict[str, Any] | None] = [None] * len(providers)

            def work(i: int, p: Any) -> None:
                tmp = app.store.create("(compare)")
                answer, error, info = "", None, {}
                tools: list[str] = []
                try:
                    for ev in app.agent.turn(tmp, text, None, [], False, p, False, {"compare": True}):
                        if ev["type"] == "text":
                            answer += ev["delta"]
                        elif ev["type"] == "tool_start":
                            tools.append(ev["name"])
                        elif ev["type"] == "error":
                            error = ev["message"]
                        elif ev["type"] in ("start", "done"):
                            info.update({k: v for k, v in ev.items() if k in ("turn_id", "ms")})
                finally:
                    app.store.delete(tmp)
                results[i] = {"model": f"{p.name}:{p.model}", "name": display_name(p), "text": answer, "error": error, "tools": tools, **info}

            threads = [threading.Thread(target=work, args=(i, p), daemon=True) for i, p in enumerate(providers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=300)  # a model on this PC can take minutes
            self._json(200, {"results": [r or {"model": f"{p.name}:{p.model}", "name": display_name(p), "text": "", "error": "timed out", "tools": []}
                                         for r, p in zip(results, providers)]})

        def _vote(self, data: dict[str, Any]) -> None:
            turn_id = str(data.get("turn_id", ""))[:40]
            if not turn_id:
                return self._json(400, {"error": "send the turn_id of the better answer"})
            app.store.save_feedback(turn_id, "compare", "up", "chosen as the better answer in a side-by-side comparison")
            if app.tracing and app.tracing.state != "off":
                threading.Thread(target=app.tracing.send_feedback, args=(turn_id, "up", "chosen as the better answer in a side-by-side comparison"), daemon=True).start()
            self._json(200, {"ok": True})

        def _upload(self, data: dict[str, Any]) -> None:
            cid, name = str(data.get("conversation_id", "")), str(data.get("name", "file"))[:120]
            if not app.store.exists(cid):
                return self._json(404, {"error": "conversation not found"})
            try:
                raw = base64.b64decode(str(data.get("data", "")), validate=True)
                kind, text, media_type = extract(name, raw)
            except (binascii.Error, ValueError):
                return self._json(400, {"error": "the file could not be received"})
            except FileProblem as exc:
                return self._json(422, {"error": str(exc)})
            did = app.store.add_document(cid, name, kind, media_type, text, base64.b64encode(raw).decode() if kind == "image" else "")
            self._json(201, {"id": did, "name": name, "kind": kind, "chars": len(text)})

    return Handler


_DEAD: dict[str, float] = {}  # models that said "not found" or "no longer available": skipped for ten minutes


def _alive(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.time()
    return [m for m in models if now - _DEAD.get(m["id"], 0.0) > 600 and now - _DEAD.get("provider:" + m["provider"], 0.0) > 600]


VISION_PROVIDERS = ("gemini", "anthropic", "openai", "xai", "mistral")  # can read pictures; the others (and most local models) cannot


def _fallback_chain(first: Any, limit: int = 4, needs_vision: bool = False) -> list[Any]:
    """The model the person picked, then other ready models to try if it is overloaded or out of quota (same provider first,
    the model on this PC last). With a picture attached only models that can read pictures are used."""
    ready = [m for m in _alive(_reachable(_with_discovered(available_models()))) if m["ready"]]
    if needs_vision and first.name not in VISION_PROVIDERS and any(m["provider"] in VISION_PROVIDERS for m in ready):
        first = next(provider_for(m["id"]) for m in ready if m["provider"] in VISION_PROVIDERS and provider_for(m["id"]) is not None)
    auto = router.enabled()
    healthy = [m for m in ready if not router.avoid(m["id"]) and (not needs_vision or m["provider"] in VISION_PROVIDERS)]
    fid = f"{first.name}:{first.model}"
    if auto and router.avoid(fid) and healthy:
        best = min(healthy, key=lambda m: (m["provider"] == "ollama", router.sort_key(m["id"])))
        alt = provider_for(best["id"])
        if alt is not None and alt.ready:
            first = alt
    chain = [first]
    seen = {f"{first.name}:{first.model}"}
    if auto:
        key = lambda m: (m["provider"] == "ollama", router.sort_key(m["id"]))  # noqa: E731 - healthy and quick first, the model on this PC last
    else:
        key = lambda m: (m["provider"] != first.name, m["provider"] == "ollama")  # noqa: E731
    ordered = sorted((m for m in ready if not needs_vision or m["provider"] in VISION_PROVIDERS), key=key)
    for m in ordered:
        if len(chain) >= limit:
            break
        if m["id"] in seen:
            continue
        p = provider_for(m["id"])
        if p is not None and p.ready:
            chain.append(p)
            seen.add(m["id"])
    return chain


def _with_fallback(agent: ChatAgent, cid: str, text: str, cancel: Any, attachments: list[str], research: bool, provider: Any, redo: bool):
    """Run a turn. If the model fails in a way another model could fix, before it wrote anything, try the next one."""
    needs_vision = any((d := agent.store.document(cid, ref)) and d.get("kind") == "image" for ref in attachments)
    chain = _fallback_chain(provider, needs_vision=needs_vision)
    if chain[0] is not provider:
        why0 = "cannot read pictures" if needs_vision and provider.name not in VISION_PROVIDERS else router.reason(f"{provider.name}:{provider.model}") or "is not the best choice right now"
        yield {"type": "notice", "message": f"{provider.model} ({display_name(provider)}) {why0}. Using {chain[0].model} ({display_name(chain[0])})."}
    for i, p in enumerate(chain):
        inner = agent.turn(cid, text, cancel, attachments, research, p, redo or i > 0)
        mid = f"{p.name}:{p.model}"
        began = time.time()
        wrote = False
        held: dict[str, Any] | None = None
        try:
            for ev in inner:
                if ev["type"] in ("text", "tool_start"):
                    wrote = True
                if ev["type"] == "done" and not research:
                    router.record_success(mid, time.time() - began)  # research answers are slow by nature, so they do not count
                if ev["type"] == "error" and ev.get("retryable"):
                    router.record_failure(mid, int(ev.get("status") or 0))
                if ev["type"] == "error" and ev.get("retryable") and not wrote and i + 1 < len(chain):
                    held = ev
                    break
                yield ev
        finally:
            inner.close()
        if held is None:
            return
        nxt = chain[i + 1]
        if held.get("status") == 404 or held.get("model_only"):
            _DEAD[f"{p.name}:{p.model}"] = time.time()  # this model only; the key and the other models are fine
        elif held.get("status") in (401, 402, 403) or "no credit" in held.get("message", "") or "not valid" in held.get("message", ""):
            _DEAD["provider:" + p.name] = time.time()  # a rejected key or an empty balance affects every model of that provider
        why = {404: "that model is not available", 429: "out of quota or rate limited", 503: "overloaded", 401: "its key was rejected", 402: "no credit on the account", 403: "its key was rejected"}.get(held.get("status", 0), "it failed")
        if "no credit" in held.get("message", ""):
            why = "no credit on the account"
        yield {"type": "notice", "message": f"{p.model} ({display_name(p)}): {why}. Trying {nxt.model} ({display_name(nxt)})."}


_DISCOVERED: dict[str, Any] = {"at": 0.0, "models": []}


def _discover_gemini() -> list[str]:
    """Ask Google which chat models this key can use, so the picker offers real names instead of guesses."""
    p = provider_for("gemini")
    if p is None or not p.ready:
        return []
    try:
        r = httpx.get(p.base_url + "/models", headers={"x-goog-api-key": p.key, "authorization": f"Bearer {p.key}"}, timeout=4)
        r.raise_for_status()
        ids = [str(m.get("id", "")).removeprefix("models/") for m in r.json().get("data", [])]
    except Exception:  # noqa: BLE001 - discovery is a convenience; the configured model still works
        return []
    skip = ("embed", "tts", "image", "live", "audio", "robotics", "computer", "vision", "aqa", "learnlm", "imagen", "veo", "gemma", "preview-", "omni", "transcribe", "customtools", "antigravity", "nano")
    good = [i for i in ids if i.startswith("gemini") and not any(w in i for w in skip)]
    good.sort(key=lambda i: ("preview" in i, "flash" not in i, "lite" in i, i))
    return good[:8]


def _discover_openrouter() -> list[str]:
    p = provider_for("openrouter")
    return openrouter_free_models() if p is not None and p.ready else []


def _with_discovered(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if time.time() - _DISCOVERED["at"] > 600:
        _DISCOVERED["models"] = _discover_gemini()
        _DISCOVERED["openrouter"] = _discover_openrouter()
        _DISCOVERED["at"] = time.time()
    have = {m["id"] for m in models}
    out = list(models)
    if _DISCOVERED.get("openrouter"):  # the built-in default may have stopped being free
        out = [m for m in out if m["provider"] != "openrouter"]
    for prov, names in (("gemini", _DISCOVERED["models"]), ("openrouter", _DISCOVERED.get("openrouter") or [])):
        for name in names:
            ident = f"{prov}:{name}"
            p = provider_for(ident)
            if ident in have or p is None:
                continue
            out.append({"id": ident, "provider": prov, "model": name, "name": display_name(p), "ready": p.ready, "tools": p.tools})
    return out


_OLLAMA_SEEN: dict[str, Any] = {"at": 0.0, "up": False}


def _reachable(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Leave Ollama out of the model list when it is not running, so nobody picks it and gets a connection error."""
    if not any(m["provider"] == "ollama" for m in models):
        return models
    if time.time() - _OLLAMA_SEEN["at"] > 20:
        try:
            httpx.get(provider_for("ollama").base_url.removesuffix("/v1") + "/api/tags", timeout=0.7)
            _OLLAMA_SEEN["up"] = True
        except Exception:  # noqa: BLE001 - not running or not installed
            _OLLAMA_SEEN["up"] = False
        _OLLAMA_SEEN["at"] = time.time()
    return models if _OLLAMA_SEEN["up"] else [m for m in models if m["provider"] != "ollama"]


def check_ollama(agent: ChatAgent) -> None:
    """Ollama only answers with models you have downloaded; say so at startup instead of on the first message."""
    p = agent.provider
    if p.name != "ollama":
        return
    import httpx

    try:
        tags = httpx.get(p.base_url.removesuffix("/v1") + "/api/tags", timeout=3).json().get("models", [])
    except Exception:
        print("WARNING: Ollama is not reachable at " + p.base_url + ". Start the Ollama app, then restart this server.")
        return
    names = [m.get("name", "") for m in tags]
    if not any(n == p.model or n.split(":")[0] == p.model.split(":")[0] for n in names):
        print(f"WARNING: Ollama does not have the model '{p.model}'. Run:  ollama pull {p.model}")
        if names:
            print("         Or use one you already have, for example:  $env:CHAT_MODEL = \"" + names[0] + "\"")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("CHAT_PORT", 8001)))
    ap.add_argument("--db", default=os.environ.get("CHAT_DB", str(HERE / "chat.db")))
    args = ap.parse_args()

    for warning in problems(HERE):
        print("WARNING: " + warning)
    ready = [m["provider"] for m in available_models() if m["ready"]]
    missing = [m["provider"] for m in available_models(include_unready=True) if not m["ready"] and m["provider"] != "ollama"]
    print("Providers with a key (shown in the model list): " + (", ".join(dict.fromkeys(ready)) or "none"))
    if missing:
        print("No key for: " + ", ".join(dict.fromkeys(missing)) + "  (add NAME_API_KEY=... to .env to enable them)")
    print("Settings read from: " + (", ".join(_ENV_FILES) if _ENV_FILES else "no .env file found (using only what is set in this window)"))
    if not os.environ.get("AUDITOR_URL"):
        print("Tracing is OFF: AUDITOR_URL is not set. Add AUDITOR_URL=http://127.0.0.1:8000 to your .env file to turn it on.")
    store = Store(args.db)
    toolbox = Toolbox(notes_index=_load_notes_index(), store=store)
    agent = ChatAgent(store, toolbox)
    tracing = Tracing(os.environ.get("AUDITOR_URL"), os.environ.get("AUDITOR_AGENT", "chat-agent"), os.environ.get("AUDITOR_API_KEY"),
                      [t["name"] for t in toolbox.defs], agent.provider)
    agent.auditor, agent.version_for = tracing.auditor, tracing.version_for
    if tracing.connect():
        print(f"Tracing ON: every chat turn goes to {tracing.url} as agent '{tracing.agent_name}' (version {tracing.label(agent.provider)}).")
    elif tracing.url:
        print(f"Tracing is not connected yet: {tracing.error}\nThe chat works; it keeps retrying in the background until the Auditor server is up.")
        tracing.start_background()
    prov = agent.provider
    if prov.key_env and prov.key:
        print(f"Using {prov.key_env}: {len(prov.key)} characters, starts with '{prov.key[:4]}'." + (
            "  WARNING: that looks like a placeholder, not a real key." if prov.looks_like_placeholder else ""))
    if not agent.has_key:
        print(f"WARNING: {agent.provider.key_env} is not set. The page will open but cannot answer until you set it and restart.")
    check_ollama(agent)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(App(agent, agent.store, args.port, tracing)))
    server.daemon_threads = True
    print(f"Chat is running at http://127.0.0.1:{args.port}  ({agent.provider.name}, model {agent.model}; tools: {', '.join(t['name'] for t in toolbox.defs)})")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if agent.auditor:
            agent.auditor.flush(timeout=5)


if __name__ == "__main__":
    main()
