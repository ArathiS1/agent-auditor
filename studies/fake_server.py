"""A scripted OpenAI-compatible server for dry runs (--fake). It answers the study questions from the answer key, so the
whole pipeline can be checked without an API key. Models whose name contains "weak" misbehave in a fixed, repeatable way:
about 15 percent of questions get "I am not sure." and about 25 percent get a confident wrong answer. Every number from a
fake run is a property of this script, never of any real model, and the report says so."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request

from taskset import TASKS


def _roll(model: str, question: str) -> int:
    return int(hashlib.sha256(f"{model}|{question}".encode()).hexdigest(), 16) % 100


def _text(messages: list) -> str:
    return json.dumps(messages, ensure_ascii=False)


def make_app() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        model, messages = str(body.get("model", "")), body.get("messages", [])
        names = [t["function"]["name"] for t in body.get("tools") or []]
        dump = _text(messages)
        task = max((t for t in TASKS if t[0] in dump), key=lambda t: len(t[0]), default=None)
        time.sleep(0.03 if "weak" not in model else 0.04)
        second = any(m.get("role") in ("tool", "tool-response") for m in messages) or "Observation" in dump or "Calling tools" in dump
        message: dict
        if task is None:
            message = {"role": "assistant", "content": "I am not sure."}
        elif not second and names:
            tool_name = task[1]
            message = _call(tool_name, task[2])
        else:
            roll = _roll(model, task[0])
            if "weak" in model and roll < 15:
                answer = "I am not sure."
            elif "weak" in model and roll < 40:
                answer = f"The answer is {_wrong(task[3])}."
            else:
                answer = f"The answer is {task[3]}."
            message = _call("final_answer", {"answer": answer}) if "final_answer" in names else {"role": "assistant", "content": answer}
        return {"id": "chatcmpl-" + uuid.uuid4().hex[:10], "object": "chat.completion", "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop"}],
                "usage": {"prompt_tokens": 60, "completion_tokens": 14, "total_tokens": 74}}

    return app


def _call(name: str, args: dict) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_" + uuid.uuid4().hex[:8], "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def _wrong(value: str) -> str:
    try:
        return str(round(float(value) * 1.1 + 1, 1)).rstrip("0").rstrip(".")
    except ValueError:
        return value[::-1].capitalize()


def start(port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(make_app(), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    return server
