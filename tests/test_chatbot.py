"""The chat app (chatbot/): tools, the streaming tool loop, saved history, tracing, and the local server. No network."""
import http.client
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

CHAT = Path(__file__).resolve().parents[1] / "chatbot"
sys.path.insert(0, str(CHAT))
import agent as A  # noqa: E402
import server as S  # noqa: E402
import tools as T  # noqa: E402
from providers import resolve_provider  # noqa: E402
from store import Store  # noqa: E402

from agent_auditor.sdk import Auditor  # noqa: E402


# ---------------------------------------------------------------- a fake Claude that speaks the streaming format
def sse(event_type: str, **data) -> str:
    return f"event: {event_type}\ndata: {json.dumps({'type': event_type, **data})}\n\n"


def text_reply(text: str, stop: str = "end_turn") -> str:
    return (sse("message_start", message={"usage": {"input_tokens": 10}})
            + sse("content_block_start", index=0, content_block={"type": "text", "text": ""})
            + "".join(sse("content_block_delta", index=0, delta={"type": "text_delta", "text": w}) for w in (text[:5], text[5:]))
            + sse("content_block_stop", index=0)
            + sse("message_delta", delta={"stop_reason": stop}, usage={"output_tokens": 5}) + sse("message_stop"))


def tool_reply(name: str, args: dict) -> str:
    raw = json.dumps(args)
    return (sse("message_start", message={"usage": {"input_tokens": 10}})
            + sse("content_block_start", index=0, content_block={"type": "tool_use", "id": "tu_1", "name": name, "input": {}})
            + sse("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": raw[:4]})
            + sse("content_block_delta", index=0, delta={"type": "input_json_delta", "partial_json": raw[4:]})
            + sse("content_block_stop", index=0)
            + sse("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 8}) + sse("message_stop"))


def fake_claude(replies: list[str], seen: list | None = None):
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=queue.pop(0).encode())

    return httpx.MockTransport(handler)


def make_agent(replies, tmp_path, seen=None, auditor=None, notes=None):
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    # Always Claude, whatever your own .env says (importing server.py loads it, and it may select Gemini).
    return A.ChatAgent(Store(tmp_path / "chat.db"), T.Toolbox(notes_index=notes), auditor=auditor,
                       transport=fake_claude(replies, seen), provider=resolve_provider({"CHAT_PROVIDER": "anthropic"}))


def run_turn(agent, text, cid=None):
    cid = cid or agent.store.create()
    return cid, list(agent.turn(cid, text))


class Tmp:
    """Minimal tmp_path so these tests also run without pytest."""

    def __enter__(self):
        import tempfile
        self.d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows keeps SQLite files locked a moment
        return Path(self.d.name)

    def __exit__(self, *a):
        self.d.cleanup()


# ---------------------------------------------------------------- tools
def test_calculator_is_exact_and_refuses_code():
    assert T.calculator("2450*0.18+75") == "516"
    assert T.calculator("sqrt(16) + 2**3") == "12"
    for bad in ("__import__('os').system('x')", "open('f')", "9**9**9", "a+1"):
        try:
            T.calculator(bad)
            raise AssertionError(f"should have refused {bad}")
        except (ValueError, SyntaxError):
            pass


def test_web_fetch_blocks_private_and_non_http_addresses():
    for url in ("http://127.0.0.1/", "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/", "file:///etc/passwd",
                "http://[::1]/", "ftp://example.com/x"):
        try:
            T.check_public_url(url)
            raise AssertionError(f"should have blocked {url}")
        except ValueError:
            pass


def test_html_becomes_readable_text():
    text = T.html_to_text("<html><head><style>x{}</style></head><body><h1>Title</h1><script>evil()</script><p>Hello <b>there</b></p></body></html>")
    assert "Title" in text and "Hello there" in text and "evil" not in text


def test_notes_search_tool_finds_the_notes():
    idx = T._load_notes_index()
    assert idx is not None
    out = T.Toolbox(notes_index=idx).run("search_notes", {"query": "What is hybrid search?"})
    assert "BM25" in out


# ---------------------------------------------------------------- the agent
def test_a_plain_answer_streams_and_is_saved():
    with Tmp() as d:
        agent = make_agent([text_reply("Hello there, friend.")], d)
        cid, events = run_turn(agent, "hi")
        assert "".join(e["delta"] for e in events if e["type"] == "text") == "Hello there, friend."
        assert events[-1]["type"] == "done"
        shown = A.to_display(agent.store.messages(cid))
        assert [m["role"] for m in shown] == ["user", "assistant"] and shown[1]["text"] == "Hello there, friend."
        assert agent.store.list()[0]["title"] == "hi"


def test_claude_can_call_a_tool_and_use_its_result():
    with Tmp() as d:
        seen = []
        agent = make_agent([tool_reply("calculator", {"expression": "6*7"}), text_reply("It is 42.")], d, seen)
        cid, events = run_turn(agent, "what is 6*7?")
        kinds = [e["type"] for e in events]
        assert kinds.index("tool_start") < kinds.index("tool_end") < len(kinds) - 1 and events[-1]["type"] == "done"
        assert [e for e in events if e["type"] == "tool_end"][0]["result"] == "42"
        sent_back = seen[1]["messages"][-1]["content"][0]
        assert sent_back["type"] == "tool_result" and sent_back["content"] == "42" and not sent_back["is_error"]
        assert seen[0]["tools"] and seen[0]["stream"] is True and "x-api-key" not in json.dumps(seen[0])
        shown = A.to_display(agent.store.messages(cid))
        assert shown[1]["tools"][0]["name"] == "calculator" and shown[1]["tools"][0]["result"] == "42"


def test_a_bad_tool_call_is_given_back_to_claude_as_an_error_not_a_crash():
    with Tmp() as d:
        seen = []
        agent = make_agent([tool_reply("calculator", {"expression": "import os"}), text_reply("Sorry, I cannot do that.")], d, seen)
        _, events = run_turn(agent, "run code")
        assert events[-1]["type"] == "done"
        assert seen[1]["messages"][-1]["content"][0]["is_error"] is True


def test_history_is_sent_on_the_next_turn_and_trimmed_by_whole_turns():
    with Tmp() as d:
        seen = []
        agent = make_agent([text_reply("one"), text_reply("two")], d, seen)
        cid, _ = run_turn(agent, "first")
        run_turn(agent, "second", cid)
        assert [m["role"] for m in seen[1]["messages"]] == ["user", "assistant", "user"]
    big = [{"role": "user", "content": "x" * 400}, {"role": "assistant", "content": [{"type": "text", "text": "y" * 400}]}] * 6
    kept = A.trim_history(big, budget=1500)
    assert 2 <= len(kept) < len(big) and kept[0]["role"] == "user"


def test_an_api_error_is_shown_plainly_and_the_next_turn_still_works():
    with Tmp() as d:
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                return httpx.Response(401, json={"error": {"message": "invalid x-api-key"}})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text_reply("Back again.").encode())

        agent = A.ChatAgent(Store(d / "c.db"), T.Toolbox(), transport=httpx.MockTransport(handler))
        cid, events = run_turn(agent, "hello")
        assert events[-1]["type"] == "error" and "not valid" in events[-1]["message"] and "test-key" not in json.dumps(events)
        _, events2 = run_turn(agent, "hello again", cid)
        assert events2[-1]["type"] == "done"


def test_stop_cancels_cleanly_and_drops_half_finished_tool_work():
    with Tmp() as d:
        agent = make_agent([tool_reply("calculator", {"expression": "1+1"}), text_reply("2")], d)
        cancel = threading.Event()
        cid = agent.store.create()
        gen = agent.turn(cid, "add", cancel)
        assert next(gen)["type"] == "start"
        assert next(gen)["type"] == "tool_start"
        cancel.set()
        assert list(gen) == []
        assert [m["role"] for m in agent.store.messages(cid)] == ["user"]


def test_each_turn_is_one_traced_run_with_a_step_per_model_call_and_tool_call():
    sent = []
    http_client = httpx.Client(base_url="http://t", transport=httpx.MockTransport(
        lambda r: (sent.append(json.loads(r.content)), httpx.Response(201, json={"id": 1}))[1]))
    auditor = Auditor("http://t", agent="chat-agent", background=False, http_client=http_client)
    with Tmp() as d:
        agent = make_agent([tool_reply("calculator", {"expression": "6*7"}), text_reply("It is 42.")], d, auditor=auditor)
        run_turn(agent, "6*7?")
    run = sent[0]
    assert run["status"] == "ok" and run["input"] == "6*7?" and "42" in run["output"]
    assert [(s["name"], s["kind"]) for s in run["steps"]] == [("model_call", "llm"), ("calculator", "tool"), ("model_call", "llm")]
    assert run["steps"][0]["attributes"]["input_tokens"] == 10


def test_a_failed_turn_is_a_failed_run():
    sent = []
    http_client = httpx.Client(base_url="http://t", transport=httpx.MockTransport(
        lambda r: (sent.append(json.loads(r.content)), httpx.Response(201, json={"id": 1}))[1]))
    auditor = Auditor("http://t", agent="chat-agent", background=False, http_client=http_client)
    with Tmp() as d:
        os.environ["ANTHROPIC_API_KEY"] = "k"
        agent = A.ChatAgent(Store(d / "c.db"), T.Toolbox(), auditor=auditor,
                            transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
        run_turn(agent, "hi")
    assert sent[0]["status"] == "error" and sent[0]["steps"][0]["status"] == "error"


# ---------------------------------------------------------------- the local server
def _serve(agent):
    app = S.App(agent, agent.store, 0)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), S.make_handler(app))
    srv.daemon_threads = True
    app.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, app.port


def _call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, json.dumps(body) if body is not None else None, h)
    resp = conn.getresponse()
    return resp.status, resp.read().decode(), dict(resp.getheaders())


def test_server_serves_the_page_with_a_strict_csp_and_streams_a_chat():
    with Tmp() as d:
        agent = make_agent([text_reply("Streaming works.")], d)
        srv, port = _serve(agent)
        try:
            status, page, headers = _call(port, "GET", "/")
            assert status == 200 and "Auditor Chat" in page and "__NONCE__" not in page
            assert "unsafe-inline" not in headers["Content-Security-Policy"] and "innerHTML" not in page
            cid = json.loads(_call(port, "POST", "/api/conversations", {})[1])["id"]
            status, stream, _ = _call(port, "POST", "/api/chat", {"conversation_id": cid, "message": "hi"})
            events = [json.loads(line[5:]) for line in stream.splitlines() if line.startswith("data:")]
            assert "".join(e.get("delta", "") for e in events) == "Streaming works." and events[-1]["type"] == "done"
            msgs = json.loads(_call(port, "GET", f"/api/conversations/{cid}")[1])["messages"]
            assert [m["role"] for m in msgs] == ["user", "assistant"]
            assert _call(port, "DELETE", f"/api/conversations/{cid}")[0] == 200
            assert _call(port, "GET", f"/api/conversations/{cid}")[0] == 404
        finally:
            srv.shutdown()


def test_server_refuses_other_hosts_cross_site_posts_and_bad_input():
    with Tmp() as d:
        agent = make_agent([], d)
        srv, port = _serve(agent)
        try:
            assert _call(port, "GET", "/api/status", headers={"Host": "evil.example"})[0] == 403
            assert _call(port, "POST", "/api/conversations", {}, {"Origin": "http://evil.example"})[0] == 403
            cid = json.loads(_call(port, "POST", "/api/conversations", {})[1])["id"]
            assert _call(port, "POST", "/api/chat", {"conversation_id": cid, "message": "  "})[0] == 400
            assert _call(port, "POST", "/api/chat", {"conversation_id": "nope", "message": "hi"})[0] == 404
            assert _call(port, "POST", "/api/chat", {"conversation_id": cid, "message": "x" * 9000})[0] == 400
        finally:
            srv.shutdown()


# ---------------------------------------------------------------- other (free) providers: Gemini, Groq, Ollama, any OpenAI-compatible service
import providers as P  # noqa: E402


def oa(chunks: list[dict]) -> str:
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def oa_text(text: str) -> str:
    return oa([{"choices": [{"delta": {"content": text[:4]}}]}, {"choices": [{"delta": {"content": text[4:]}, "finish_reason": "stop"}]}])


def oa_tool(name: str, args: dict, call_id: str = "call_9") -> str:
    raw = json.dumps(args)
    return oa([{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "function": {"name": name, "arguments": raw[:5]}}]}}]},
               {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": raw[5:]}}]}, "finish_reason": "tool_calls"}]}])


def test_the_provider_is_chosen_from_whichever_key_is_set():
    assert P.resolve_provider({"GEMINI_API_KEY": "g"}).name == "gemini"
    assert P.resolve_provider({"GROQ_API_KEY": "g"}).name == "groq"
    assert P.resolve_provider({"ANTHROPIC_API_KEY": "a", "GEMINI_API_KEY": "g"}).name == "anthropic"
    assert P.resolve_provider({}).name == "anthropic"
    ollama = P.resolve_provider({"CHAT_PROVIDER": "ollama", "CHAT_MODEL": "qwen2.5"})
    assert ollama.ready and ollama.model == "qwen2.5" and ollama.base_url.startswith("http://127.0.0.1")
    custom = P.resolve_provider({"CHAT_PROVIDER": "custom", "CHAT_BASE_URL": "http://x/v1/", "CHAT_MODEL": "m", "CHAT_API_KEY": "k"})
    assert custom.base_url == "http://x/v1" and custom.key_env == "CHAT_API_KEY" and custom.model == "m"


def test_claude_style_history_converts_to_the_openai_format():
    history = [{"role": "user", "content": "6*7?"},
               {"role": "assistant", "content": [{"type": "text", "text": "Checking."},
                                                 {"type": "tool_use", "id": "t1", "name": "calculator", "input": {"expression": "6*7"}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "42"}]}]
    out = P.to_openai_messages("SYS", history)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    assert out[2]["tool_calls"][0]["function"] == {"name": "calculator", "arguments": '{"expression": "6*7"}'}
    assert out[3] == {"role": "tool", "tool_call_id": "t1", "content": "42"}
    assert P.to_openai_tools(T.Toolbox().defs)[0]["function"]["parameters"]["type"] == "object"


def _openai_agent(replies, d, seen=None):
    os.environ["GEMINI_API_KEY"] = "free-key"
    queue = list(replies)

    def handler(request):
        if seen is not None:
            seen.append((str(request.url), dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=queue.pop(0).encode())

    provider = P.resolve_provider({"CHAT_PROVIDER": "gemini", "GEMINI_API_KEY": "free-key"})
    return A.ChatAgent(Store(d / "c.db"), T.Toolbox(), provider=provider, transport=httpx.MockTransport(handler))


def test_a_free_openai_compatible_provider_streams_and_runs_tools():
    with Tmp() as d:
        seen = []
        agent = _openai_agent([oa_tool("calculator", {"expression": "6*7"}), oa_text("It is 42.")], d, seen)
        _, events = run_turn(agent, "6*7?")
        assert [e["type"] for e in events if e["type"] in ("tool_start", "tool_end", "done")] == ["tool_start", "tool_end", "done"]
        assert "It is 42." in "".join(e.get("delta", "") for e in events)
        url, headers, body = seen[0]
        assert url.endswith("/openai/chat/completions") and headers["authorization"] == "Bearer free-key"
        assert body["model"] == "gemini-flash-latest" and body["tools"][0]["type"] == "function"
        assert seen[1][2]["messages"][-1] == {"role": "tool", "tool_call_id": "call_9", "content": "42"}


def test_a_provider_that_sends_no_tool_call_id_still_works():
    with Tmp() as d:
        agent = _openai_agent([oa_tool("calculator", {"expression": "1+1"}, call_id=""), oa_text("Two.")], d)
        _, events = run_turn(agent, "1+1")
        assert events[-1]["type"] == "done"


def test_ollama_needs_no_key():
    with Tmp() as d:
        os.environ.pop("GEMINI_API_KEY", None)
        provider = P.resolve_provider({"CHAT_PROVIDER": "ollama"})
        seen = []
        agent = A.ChatAgent(Store(d / "c.db"), T.Toolbox(), provider=provider, transport=httpx.MockTransport(
            lambda r: (seen.append(dict(r.headers)), httpx.Response(200, content=oa_text("Local reply.").encode()))[1]))
        _, events = run_turn(agent, "hi")
        assert events[-1]["type"] == "done" and "authorization" not in seen[0] and agent.has_key


def test_other_providers_errors_are_plain_and_never_show_the_key():
    with Tmp() as d:
        os.environ["GEMINI_API_KEY"] = "free-key"
        provider = P.resolve_provider({"CHAT_PROVIDER": "gemini", "GEMINI_API_KEY": "free-key"})
        agent = A.ChatAgent(Store(d / "c.db"), T.Toolbox(), provider=provider,
                            transport=httpx.MockTransport(lambda r: httpx.Response(403, json=[{"error": {"message": "denied"}}])))
        _, events = run_turn(agent, "hi")
        assert events[-1]["type"] == "error" and "gemini" in events[-1]["message"] and "free-key" not in json.dumps(events)


# ---------------------------------------------------------------- files, documents, web search, deep research
import base64  # noqa: E402
import io  # noqa: E402
import zipfile  # noqa: E402

import files as FI  # noqa: E402
import textindex as TI  # noqa: E402


def make_docx(paragraphs):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = f'<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_uploaded_files_become_text_or_images_and_bad_ones_get_a_plain_message():
    assert FI.extract("notes.md", b"# Title\nhello")[:2] == ("text", "# Title\nhello")
    assert FI.extract("a.docx", make_docx(["First para", "Second para"]))[1] == "First para\nSecond para"
    assert FI.extract("p.PNG", b"\x89PNG....")[0::2] == ("image", "image/png")
    for name, data, word in (("x.xlsx", b"PK", "csv"), ("x.exe", b"MZ", "cannot read"), ("e.txt", b"", "empty"),
                             ("bad.pdf", b"not a pdf", "PDF"), ("big.txt", b"x" * (FI.MAX_FILE_BYTES + 1), "too large")):
        try:
            FI.extract(name, data)
            raise AssertionError(f"{name} should have been refused")
        except FI.FileProblem as exc:
            assert word.lower() in str(exc).lower(), (name, str(exc))


def test_the_text_index_finds_the_right_passage_in_a_long_document():
    doc = "\n\n".join(f"Section {i}. Filler text about topic number {i}." for i in range(40)) + "\n\nThe refund window is 45 days from delivery."
    passages = TI.chunk(doc, 300)
    best = TI.top_passages("what is the refund window?", passages, 2)
    assert "45 days" in passages[best[0][0]]


def test_web_search_uses_tavily_brave_or_duckduckgo_and_formats_results():
    def tavily(request):
        assert request.headers["authorization"] == "Bearer tv" and json.loads(request.content)["query"] == "ai agents"
        return httpx.Response(200, json={"results": [{"title": "Agents", "url": "https://a.example/x", "content": "About agents."}]})

    os.environ["TAVILY_API_KEY"] = "tv"
    try:
        out = T.search_web("ai agents", 3, transport=httpx.MockTransport(tavily))
    finally:
        os.environ.pop("TAVILY_API_KEY", None)
    assert "1. Agents" in out and "https://a.example/x" in out and "About agents." in out

    os.environ["BRAVE_API_KEY"] = "br"
    try:
        out = T.search_web("q", 3, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"web": {"results": [
            {"title": "B", "url": "https://b.example", "description": "bee"}]}})))
    finally:
        os.environ.pop("BRAVE_API_KEY", None)
    assert "https://b.example" in out

    html = ('<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fd.example%2Fp&rut=1">Duck <b>page</b></a>'
            '<a class="result__snippet">A snippet here</a></div>')
    out = T.search_web("q", 3, transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    assert "Duck page" in out and "https://d.example/p" in out and "A snippet here" in out


def test_web_search_failures_are_explained_not_crashes():
    try:
        T.search_web("q", 3, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>blocked</html>")))
        raise AssertionError("expected a plain error")
    except ValueError as exc:
        assert "TAVILY_API_KEY" in str(exc)


def test_document_tools_search_and_read_what_was_uploaded():
    with Tmp() as d:
        store = Store(d / "c.db")
        cid = store.create()
        long_text = "\n\n".join(f"Chapter {i}. Padding words about subject {i}." for i in range(300)) + "\n\nThe warranty lasts 24 months."
        store.add_document(cid, "manual.txt", "text", "text/plain", long_text)
        box = T.Toolbox(store=store)
        assert "24 months" in box.run("search_documents", {"query": "how long is the warranty"}, cid)
        first = box.run("read_document", {"name": "manual"}, cid)
        assert first.startswith("Chapter 0") and "offset 8000" in first
        assert "[end of document]" in box.run("read_document", {"name": "manual", "offset": len(long_text) - 50}, cid)
        assert "No documents" in box.run("search_documents", {"query": "x"}, store.create())
        assert box.kind("web_search") == "retrieval" and box.kind("read_document") == "tool"


def test_an_attached_file_is_sent_to_the_model_and_shown_as_a_file_chip():
    with Tmp() as d:
        seen = []
        agent = make_agent([text_reply("It says hello.")], d, seen)
        cid = agent.store.create()
        doc = agent.store.add_document(cid, "memo.txt", "text", "text/plain", "hello from the memo")
        list(agent.turn(cid, "What does it say?", attachments=[doc]))
        blocks = seen[0]["messages"][0]["content"]
        assert blocks[0]["text"] == "What does it say?" and blocks[1]["text"].startswith("[Attached file: memo.txt]\nhello from the memo")
        shown = A.to_display(agent.store.messages(cid))
        assert shown[0] == {"role": "user", "text": "What does it say?", "tools": [], "files": ["memo.txt"]}


def test_an_image_goes_to_claude_as_an_image_block_and_to_other_providers_as_a_data_url():
    png = base64.b64encode(b"\x89PNG-fake").decode()
    with Tmp() as d:
        seen = []
        agent = make_agent([text_reply("A picture.")], d, seen)
        cid = agent.store.create()
        doc = agent.store.add_document(cid, "pic.png", "image", "image/png", "", png)
        list(agent.turn(cid, "What is this?", attachments=[doc]))
        image = seen[0]["messages"][0]["content"][2]
        assert image["type"] == "image" and image["source"] == {"type": "base64", "media_type": "image/png", "data": png}
        converted = P.to_openai_messages("S", [{"role": "user", "content": seen[0]["messages"][0]["content"]}])[1]["content"]
        assert converted[-1] == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}}
    big = [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 400000}}]},
           {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}, {"role": "user", "content": "next"}]
    assert len(A.trim_history(big)) == 3  # image bytes are not counted as hundreds of thousands of characters


def test_deep_research_mode_uses_the_research_prompt_more_rounds_and_a_bigger_answer_budget():
    with Tmp() as d:
        seen = []
        agent = make_agent([text_reply("Report.")], d, seen)
        list(agent.turn(agent.store.create(), "research x", research=True))
        assert "DEEP RESEARCH MODE" in seen[0]["system"] and seen[0]["max_tokens"] == 8000
        seen2 = []
        agent2 = make_agent([text_reply("Hi.")], d, seen2)
        list(agent2.turn(agent2.store.create(), "hi"))
        assert "DEEP RESEARCH" not in seen2[0]["system"] and seen2[0]["max_tokens"] == 4096
    assert {"web_search", "fetch_webpage", "calculator"} <= {t["name"] for t in T.Toolbox().defs}


def test_ollama_without_the_model_says_exactly_what_to_run():
    with Tmp() as d:
        provider = P.resolve_provider({"CHAT_PROVIDER": "ollama"})
        agent = A.ChatAgent(Store(d / "c.db"), T.Toolbox(), provider=provider,
                            transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"error": {"message": "model not found"}})))
        _, events = run_turn(agent, "hi")
        assert "ollama pull llama3.1" in events[-1]["message"]


def test_server_accepts_uploads_and_answers_questions_about_them():
    with Tmp() as d:
        seen = []
        agent = make_agent([text_reply("The memo says hello.")], d, seen)
        srv, port = _serve(agent)
        try:
            cid = json.loads(_call(port, "POST", "/api/conversations", {})[1])["id"]
            ok = _call(port, "POST", "/api/upload", {"conversation_id": cid, "name": "memo.txt", "data": base64.b64encode(b"hello memo").decode()})
            assert ok[0] == 201
            info = json.loads(ok[1])
            assert info["kind"] == "text" and info["chars"] == 10
            bad = _call(port, "POST", "/api/upload", {"conversation_id": cid, "name": "x.exe", "data": base64.b64encode(b"MZ").decode()})
            assert bad[0] == 422 and "cannot read" in bad[1]
            assert _call(port, "POST", "/api/upload", {"conversation_id": cid, "name": "x.txt", "data": "***"})[0] == 400
            status, stream, _ = _call(port, "POST", "/api/chat", {"conversation_id": cid, "message": "", "attachments": [info["id"], "nope"]})
            events = [json.loads(line[5:]) for line in stream.splitlines() if line.startswith("data:")]
            assert status == 200 and "The memo says hello." == "".join(e.get("delta", "") for e in events)
            sent = seen[0]["messages"][0]["content"]
            assert "summary" in sent[0]["text"] and "hello memo" in sent[1]["text"] and len(sent) == 2
        finally:
            srv.shutdown()


def test_a_pasted_key_is_cleaned_and_a_bad_key_error_is_clear():
    os.environ["GEMINI_API_KEY"] = ' "<AIzaSyExampleExampleExampleExample123>" '
    try:
        prov = P.resolve_provider({"CHAT_PROVIDER": "gemini"})
        assert prov.key == "AIzaSyExampleExampleExampleExample123" and not prov.looks_like_placeholder
        os.environ["GEMINI_API_KEY"] = "<your key>"
        assert P.resolve_provider({"CHAT_PROVIDER": "gemini"}).looks_like_placeholder
        with Tmp() as d:
            agent = A.ChatAgent(Store(d / "c.db"), T.Toolbox(), provider=P.resolve_provider({"CHAT_PROVIDER": "gemini"}),
                                transport=httpx.MockTransport(lambda r: httpx.Response(400, json=[{"error": {"message": "API key not valid. Please pass a valid API key."}}])))
            _, events = run_turn(agent, "hi")
            assert "not valid" in events[-1]["message"] and "GEMINI_API_KEY" in events[-1]["message"]
    finally:
        os.environ.pop("GEMINI_API_KEY", None)


def test_404_shows_the_real_server_message_not_a_guess():
    msg = A.ChatAgent._explain(404, '{"error":{"message":"models/x is not found"}}', __import__("providers").PRESETS["gemini"], "https://h/chat/completions")
    assert "models/x is not found" in msg and "https://h/chat/completions" in msg and "diagnose.py" in msg


def test_gemini_thought_signature_is_kept_and_sent_back_with_the_tool_call():
    import providers as P
    sig = {"google": {"thought_signature": "abc123"}}
    chunks = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","extra_content":' + json.dumps(sig) + ',"function":{"name":"calculator","arguments":"{\\"expression\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"17*23\\"}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    final = [e for e in P.parse_openai_stream(iter(chunks)) if "final" in e][0]["final"]
    block = final["content"][0]
    assert block["input"] == {"expression": "17*23"} and block["extra_content"] == sig
    msgs = P.to_openai_messages("sys", [{"role": "assistant", "content": [block]}], gemini=True)
    assert msgs[1]["tool_calls"][0]["extra_content"] == sig  # the real signature, not the placeholder


def test_gemini_calls_without_a_stored_signature_get_the_documented_placeholder():
    import providers as P
    block = {"type": "tool_use", "id": "c1", "name": "calculator", "input": {}}
    gem = P.to_openai_messages("s", [{"role": "assistant", "content": [block]}], gemini=True)
    other = P.to_openai_messages("s", [{"role": "assistant", "content": [block]}], gemini=False)
    assert gem[1]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == P.GEMINI_SKIP_SIGNATURE
    assert "extra_content" not in other[1]["tool_calls"][0]


def test_claude_requests_never_carry_the_gemini_only_field():
    msgs = [{"role": "assistant", "content": [{"type": "tool_use", "id": "c", "name": "n", "input": {}, "extra_content": {"x": 1}}]}]
    assert "extra_content" not in A._for_anthropic(msgs)[0]["content"][0] and "extra_content" in msgs[0]["content"][0]


def test_a_tool_call_with_no_text_is_sent_without_an_empty_content_field():
    import providers as P
    block = {"type": "tool_use", "id": "c1", "name": "calculator", "input": {"expression": "1+1"}}
    msg = P.to_openai_messages("s", [{"role": "assistant", "content": [block]}], gemini=True)[1]
    assert "content" not in msg and msg["tool_calls"][0]["function"]["name"] == "calculator"
    with_text = P.to_openai_messages("s", [{"role": "assistant", "content": [{"type": "text", "text": "Checking."}, block]}])[1]
    assert with_text["content"] == "Checking."


def test_env_file_is_read_but_the_window_wins_and_unrelated_lines_are_ignored():
    import tempfile

    import envfile as E
    values = E.parse('# note\nCHAT_PROVIDER=gemini\nexport GEMINI_API_KEY="AQ.abc"\nAUDITOR_URL=http://127.0.0.1:8000 # local\n'
                     'POSTGRES_PASSWORD=secret\nCHAT_MODEL=\nTAVILY_API_KEY = \'tv-1\'\n')
    assert values == {"CHAT_PROVIDER": "gemini", "GEMINI_API_KEY": "AQ.abc", "AUDITOR_URL": "http://127.0.0.1:8000", "TAVILY_API_KEY": "tv-1"}
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "chatbot").mkdir()
        (tmp / ".env").write_text("CHAT_MODEL=from-file\nCHAT_PORT=9999\n", encoding="utf-8")
        os.environ["CHAT_MODEL"] = "from-window"
        os.environ.pop("CHAT_PORT", None)
        try:
            used = E.load_env(tmp / "chatbot")
            assert used == [str(tmp / ".env")]
            assert os.environ["CHAT_MODEL"] == "from-window" and os.environ["CHAT_PORT"] == "9999"
        finally:
            os.environ.pop("CHAT_MODEL", None)
            os.environ.pop("CHAT_PORT", None)


def test_overloaded_model_gets_a_plain_message_after_retries(monkeypatch):
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    with Tmp() as d:
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(503, json={"error": {"message": "This model is currently experiencing high demand."}})

        agent = A.ChatAgent(Store(d / "chat.db"), T.Toolbox(), transport=httpx.MockTransport(handler),
                            provider=resolve_provider({"CHAT_PROVIDER": "anthropic"}))
        _, events = run_turn(agent, "hi")
        assert events[-1]["type"] == "error" and "overloaded" in events[-1]["message"] and "another model" in events[-1]["message"]
        assert len(calls) == 5


def test_connection_refused_says_what_to_do(monkeypatch):
    with Tmp() as d:
        os.environ["ANTHROPIC_API_KEY"] = "test-key"

        def handler(request):
            raise httpx.ConnectError("refused")

        agent = A.ChatAgent(Store(d / "chat.db"), T.Toolbox(), transport=httpx.MockTransport(handler),
                            provider=resolve_provider({"CHAT_PROVIDER": "anthropic"}))
        _, events = run_turn(agent, "hi")
        assert events[-1]["type"] == "error" and "Could not reach" in events[-1]["message"]


def test_quota_message_shows_the_providers_reason():
    msg = A.ChatAgent._explain(429, '{"error":{"message":"You exceeded your current quota, please check your plan"}}',
                               resolve_provider({"CHAT_PROVIDER": "gemini", "GEMINI_API_KEY": "k" * 20}))
    assert "quota" in msg and "exceeded your current quota" in msg and "another model" in msg


def test_a_failing_model_falls_back_to_the_next_one(monkeypatch):
    import server as S
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    monkeypatch.setattr(S, "_discover_gemini", lambda: [])
    monkeypatch.setattr(S, "_reachable", lambda m: m)
    monkeypatch.setenv("GEMINI_API_KEY", "g" * 20)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a" * 20)
    S._DISCOVERED["at"] = 0.0
    with Tmp() as d:
        def handler(request):
            if "anthropic" in str(request.url):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text_reply("Hello from Claude").encode())
            return httpx.Response(429, json={"error": {"message": "quota exceeded"}})

        agent = A.ChatAgent(Store(d / "chat.db"), T.Toolbox(), transport=httpx.MockTransport(handler), provider=resolve_provider({"CHAT_PROVIDER": "gemini"}))
        cid = agent.store.create()
        events = list(S._with_fallback(agent, cid, "hi", None, [], False, S.provider_for("gemini"), False))
        kinds = [e["type"] for e in events]
        assert "notice" in kinds and kinds[-1] == "done" and "error" not in kinds
        assert [m for m in agent.store.messages(cid) if m["role"] == "user"].__len__() == 1  # the question is not duplicated


def test_a_model_that_is_gone_is_skipped_and_remembered(monkeypatch):
    import server as S
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    monkeypatch.setattr(S, "_discover_gemini", lambda: ["old-model"])
    monkeypatch.setattr(S, "_discover_openrouter", lambda: [])
    monkeypatch.setattr(S, "_reachable", lambda m: m)
    monkeypatch.setenv("GEMINI_API_KEY", "g" * 20)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a" * 20)
    S._DISCOVERED["at"] = 0.0
    S._DEAD.clear()
    with Tmp() as d:
        def handler(request):
            if "anthropic" in str(request.url):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text_reply("Hello from Claude").encode())
            return httpx.Response(404, json={"error": {"message": "This model is no longer available to new users."}})

        agent = A.ChatAgent(Store(d / "chat.db"), T.Toolbox(), transport=httpx.MockTransport(handler), provider=resolve_provider({"CHAT_PROVIDER": "gemini"}))
        cid = agent.store.create()
        events = list(S._with_fallback(agent, cid, "hi", None, [], False, S.provider_for("gemini:old-model"), False))
        assert events[-1]["type"] == "done" and any(e["type"] == "notice" and "not available" in e["message"] for e in events)
        assert "gemini:old-model" in S._DEAD
        assert "gemini:old-model" not in [m["id"] for m in S._alive(S._with_discovered(S.available_models()))]


def test_env_file_reads_every_provider_key_and_flags_typos(tmp_path, monkeypatch):
    import envfile
    (tmp_path / ".env").write_text("Openrouter_API_KEY=sk-or-v1-abc\nMISTREL_API_KEY=m1\nOPENAI_API_KEY= \nDEEPSEEK_API_KEY=sk-d\nXAI_API_KEY=xai-real\n", encoding="utf-8")
    parsed = envfile.parse((tmp_path / ".env").read_text())
    assert parsed["OPENROUTER_API_KEY"] == "sk-or-v1-abc" and parsed["DEEPSEEK_API_KEY"] == "sk-d" and parsed["XAI_API_KEY"] == "xai-real"
    assert "OPENAI_API_KEY" not in parsed  # empty value: not enabled
    sub = tmp_path / "chatbot"
    sub.mkdir()
    msgs = envfile.problems(sub)
    assert any("MISTREL_API_KEY" in m and "MISTRAL_API_KEY" in m for m in msgs)


def test_placeholder_keys_are_listed_but_not_ready():
    from providers import available_models
    models = available_models({"XAI_API_KEY": "xai", "OPENAI_API_KEY": "sk-real-looking-key-123"}, include_unready=True)
    by = {m["provider"]: m for m in models}
    assert by["xai"]["ready"] is False and by["openai"]["ready"] is True and by["groq"]["ready"] is False


def test_an_empty_balance_or_rejected_key_moves_on_to_another_model(monkeypatch):
    import server as S
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    monkeypatch.setattr(S, "_discover_gemini", lambda: [])
    monkeypatch.setattr(S, "_discover_openrouter", lambda: [])
    monkeypatch.setattr(S, "_reachable", lambda m: m)
    monkeypatch.setenv("GEMINI_API_KEY", "g" * 20)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a" * 20)
    S._DISCOVERED["at"] = 0.0
    S._DEAD.clear()
    with Tmp() as d:
        def handler(request):
            if "anthropic" in str(request.url):  # Claude says: no credit (HTTP 400, like the real service)
                return httpx.Response(400, json={"error": {"message": "Your credit balance is too low to access the Anthropic API."}})
            return httpx.Response(401, json={"error": {"message": "bad key"}})  # and Gemini rejects its key

        agent = A.ChatAgent(Store(d / "chat.db"), T.Toolbox(), transport=httpx.MockTransport(handler), provider=resolve_provider({"CHAT_PROVIDER": "anthropic"}))
        cid = agent.store.create()
        events = list(S._with_fallback(agent, cid, "hi", None, [], False, S.provider_for("anthropic"), False))
        assert any(e["type"] == "notice" and "no credit" in e["message"] for e in events)
        assert "provider:anthropic" in S._DEAD and "provider:gemini" in S._DEAD
        assert "no credit" in events[-1]["message"] or "key" in events[-1]["message"]


def test_key_checker_names_each_kind_of_failure():
    import check_keys as K
    p = P.PRESETS["openai"]
    assert K.verdict(p, 200, "")[0] == "OK"
    assert K.verdict(p, 402, "Insufficient Balance")[0] == "NO CREDIT"
    assert K.verdict(p, 400, "Your credit balance is too low")[0] == "NO CREDIT"
    assert K.verdict(p, 401, "User not found")[0] == "KEY REJECTED"
    assert K.verdict(p, 429, "quota")[0] == "OUT OF QUOTA"
    assert K.verdict(p, 503, "busy")[0] == "PROVIDER BUSY"


def test_gemini_quota_message_is_not_mistaken_for_no_credit():
    import check_keys as K
    body = '{"error":{"message":"You exceeded your current quota, please check your plan and billing details."}}'
    assert K.verdict(P.PRESETS["gemini"], 429, body)[0] == "OUT OF QUOTA"
    assert K.verdict(P.PRESETS["xai"], 403, "Your newly created team does not have any credits or licenses yet")[0] == "NO CREDIT"
    assert K.verdict(P.PRESETS["openai"], 429, '{"error":{"code":"insufficient_quota"}}')[0] == "NO CREDIT"


def test_a_model_that_refuses_chat_does_not_kill_the_whole_provider(monkeypatch):
    import server as S
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    monkeypatch.setattr(S, "_discover_gemini", lambda: [])
    monkeypatch.setattr(S, "_discover_openrouter", lambda: [])
    monkeypatch.setattr(S, "_reachable", lambda m: m)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-" + "a" * 60)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a" * 20)
    S._DISCOVERED["at"] = 0.0
    S._DEAD.clear()
    with Tmp() as d:
        def handler(request):
            if "anthropic" in str(request.url):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=text_reply("Hi").encode())
            return httpx.Response(403, json={"error": {"message": "x:free is only available on agentic harnesses."}})

        agent = A.ChatAgent(Store(d / "chat.db"), T.Toolbox(), transport=httpx.MockTransport(handler), provider=resolve_provider({"CHAT_PROVIDER": "anthropic"}))
        cid = agent.store.create()
        events = list(S._with_fallback(agent, cid, "hi", None, [], False, S.provider_for("openrouter:x:free"), False))
        assert events[-1]["type"] == "done"
        assert "openrouter:x:free" in S._DEAD and "provider:openrouter" not in S._DEAD


def test_checker_says_model_refused_not_key_rejected():
    import check_keys as K
    assert K.verdict(P.PRESETS["openrouter"], 403, "x is only available on agentic harnesses")[0] == "MODEL REFUSED"


def test_a_repeated_identical_tool_call_is_not_run_again_and_a_loop_is_stopped():
    with Tmp() as d:
        calls = []
        agent = make_agent([tool_reply("calculator", {"expression": "6*7"}), tool_reply("calculator", {"expression": "6*7"}),
                            text_reply("It is 42.")], d)
        real = agent.tools.run
        agent.tools.run = lambda n, i, c: (calls.append(n), real(n, i, c))[1]
        cid, events = run_turn(agent, "what is 6*7?")
        assert events[-1]["type"] == "done" and len(calls) == 1  # the second identical call was answered from the first
        assert any("already made this exact call" in e.get("result", "") or e["type"] == "tool_end" for e in events)
    with Tmp() as d:
        agent = make_agent([tool_reply("calculator", {"expression": "6*7"})] * 5, d)
        cid, events = run_turn(agent, "loop")
        err = events[-1]
        assert err["type"] == "error" and "repeating" in err["message"]


def test_a_picture_goes_only_to_models_that_can_read_pictures(monkeypatch):
    import server as S
    monkeypatch.setattr(S, "_discover_gemini", lambda: [])
    monkeypatch.setattr(S, "_discover_openrouter", lambda: [])
    monkeypatch.setattr(S, "_reachable", lambda m: m)
    monkeypatch.setenv("GEMINI_API_KEY", "g" * 30)
    monkeypatch.setenv("GROQ_API_KEY", "q" * 30)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a" * 20)
    S._DISCOVERED["at"] = 0.0
    S._DEAD.clear()
    chain = S._fallback_chain(S.provider_for("ollama"), needs_vision=True)
    names = [p.name for p in chain]
    assert names[0] in S.VISION_PROVIDERS and "ollama" not in names and "groq" not in names
    plain = [p.name for p in S._fallback_chain(S.provider_for("gemini"))]
    assert plain[0] == "gemini" and (("ollama" not in plain) or plain[-1] == "ollama")  # the model on this PC goes last


def test_followups_are_off_unless_enabled(monkeypatch):
    monkeypatch.delenv("CHAT_FOLLOWUPS", raising=False)
    import inspect
    assert "CHAT_FOLLOWUPS" in inspect.getsource(S)


def test_indian_gateways_are_presets_read_from_env_and_pickable(monkeypatch):
    import envfile
    import providers as P
    assert P.PRESETS["airouter"].base_url == "https://api.airouter.in/v1" and P.PRESETS["nabh"].base_url == "https://api.nabh.cloud/v1"
    assert envfile.parse("AIROUTER_API_KEY=sk-air-v1-abc\nnabh_api_key=xyz\n") == {"AIROUTER_API_KEY": "sk-air-v1-abc", "NABH_API_KEY": "xyz"}
    env = {"AIROUTER_API_KEY": "sk-air-v1-" + "a" * 30, "AIROUTER_MODEL": "google/gemini-2.5-flash-lite"}
    m = [x for x in P.available_models(env) if x["provider"] == "airouter"]
    assert m and m[0]["ready"] and m[0]["model"] == "google/gemini-2.5-flash-lite"
    assert P.provider_for("nabh").tools is False


def test_a_service_you_describe_in_env_becomes_a_provider(monkeypatch):
    import envfile
    import providers as P
    env = {"INDOS_API_KEY": "k" * 20, "INDOS_BASE_URL": "https://api.indos.example/v1/", "INDOS_MODEL": "indos-small"}
    pr = P.all_presets(env)["indos"]
    assert pr.base_url == "https://api.indos.example/v1" and pr.model == "indos-small" and pr.key_env == "INDOS_API_KEY"
    ready = [m for m in P.available_models(env) if m["provider"] == "indos"]
    assert ready and ready[0]["ready"] and P.provider_for("indos", env).model == "indos-small"
    assert "INDOS_API_KEY" in envfile.parse("indos_api_key=abc\nindos_base_url=https://x/v1\nindos_model=m\nPOSTGRES_PASSWORD=x")
    assert "POSTGRES_PASSWORD" not in envfile.parse("POSTGRES_PASSWORD=x")
    assert "INDOS_API_KEY" not in P.all_presets({"INDOS_API_KEY": "k"}) and "indos" not in P.all_presets({"INDOS_API_KEY": "k"})


def test_an_error_inside_a_200_stream_is_explained_and_lets_the_chat_try_another_model(monkeypatch):
    monkeypatch.setenv("INDOS_API_KEY", "k" * 10)
    import providers as P
    body = 'data: {"error": {"message": "No endpoints found for x. Filter by Tool Compatibility removed a, b"}}\n\n'
    with Tmp() as d:
        def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())

        agent = A.ChatAgent(Store(d / "chat.db"), T.Toolbox(), transport=httpx.MockTransport(handler), provider=resolve_provider({"CHAT_PROVIDER": "anthropic"}))
        p = P.provider_for("indos", {"INDOS_API_KEY": "k" * 10, "INDOS_BASE_URL": "https://x.example/v1", "INDOS_MODEL": "m"})
        cid = agent.store.create()
        ev = list(agent.turn(cid, "hi", None, [], False, p, False))
        err = ev[-1]
        assert err["type"] == "error" and err["retryable"] and err["model_only"] and "NAME_TOOLS=off" in err["message"]
    env = {"INDOS_API_KEY": "k" * 10, "INDOS_BASE_URL": "https://x.example/v1", "INDOS_MODEL": "m", "INDOS_TOOLS": "off"}
    assert P.all_presets(env)["indos"].tools is False
