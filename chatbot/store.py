"""Saves conversations in a local SQLite file so a chat survives a restart."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: str | Path) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript(
                "CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, title TEXT, created REAL, updated REAL);"
                "CREATE TABLE IF NOT EXISTS messages (n INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, "
                "role TEXT, content TEXT);"
                "CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, conversation_id TEXT, name TEXT, kind TEXT, "
                "media_type TEXT, text TEXT, data TEXT, created REAL);"
                "CREATE TABLE IF NOT EXISTS turns (n INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, turn_id TEXT, model TEXT, provider TEXT);"
                "CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, created REAL);"
                "CREATE TABLE IF NOT EXISTS feedback (turn_id TEXT PRIMARY KEY, conversation_id TEXT, rating TEXT, comment TEXT, "
                "sent INTEGER DEFAULT 0, created REAL);")

    def create(self, title: str = "New chat") -> str:
        cid = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock, self._db:
            self._db.execute("INSERT INTO conversations VALUES (?,?,?,?)", (cid, title, now, now))
        return cid

    def exists(self, cid: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM conversations WHERE id=?", (cid,)).fetchone() is not None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT id,title,updated FROM conversations ORDER BY updated DESC LIMIT 200").fetchall()
        return [{"id": r[0], "title": r[1], "updated": r[2]} for r in rows]

    def messages(self, cid: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT role,content FROM messages WHERE conversation_id=? ORDER BY n", (cid,)).fetchall()
        return [{"role": r[0], "content": json.loads(r[1])} for r in rows]

    def append(self, cid: str, role: str, content: Any) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO messages (conversation_id,role,content) VALUES (?,?,?)", (cid, role, json.dumps(content)))
            self._db.execute("UPDATE conversations SET updated=? WHERE id=?", (time.time(), cid))

    def retitle(self, cid: str, title: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE conversations SET title=? WHERE id=?", (title[:60], cid))

    # ---- uploaded files
    def add_document(self, cid: str, name: str, kind: str, media_type: str, text: str, data_b64: str = "") -> str:
        did = uuid.uuid4().hex[:10]
        with self._lock, self._db:
            self._db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)", (did, cid, name[:120], kind, media_type, text, data_b64, time.time()))
        return did

    def document(self, cid: str, ref: str) -> dict[str, Any] | None:
        """Find a document in this conversation by id or by (part of) its name."""
        for d in self.documents(cid):
            if d["id"] == ref:
                return d
        low = ref.lower()
        return next((d for d in self.documents(cid) if low and low in d["name"].lower()), None)

    def documents(self, cid: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT id,name,kind,media_type,text,data FROM documents WHERE conversation_id=? ORDER BY created", (cid,)).fetchall()
        return [{"id": r[0], "name": r[1], "kind": r[2], "media_type": r[3], "text": r[4], "data": r[5]} for r in rows]

    def delete(self, cid: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM documents WHERE conversation_id=?", (cid,))
            self._db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
            self._db.execute("DELETE FROM turns WHERE conversation_id=?", (cid,))
            self._db.execute("DELETE FROM feedback WHERE conversation_id=?", (cid,))
            self._db.execute("DELETE FROM conversations WHERE id=?", (cid,))

    def rewind(self, cid: str, keep: int) -> None:
        """Drop everything after the first `keep` messages (used to discard a half-finished turn)."""
        with self._lock, self._db:
            ids = [r[0] for r in self._db.execute("SELECT n FROM messages WHERE conversation_id=? ORDER BY n", (cid,))]
            for n in ids[keep:]:
                self._db.execute("DELETE FROM messages WHERE n=?", (n,))

    # ---- memory: short facts the person asked the assistant to keep
    def memories(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT id,text FROM memories ORDER BY id").fetchall()
        return [{"id": r[0], "text": r[1]} for r in rows]

    def add_memory(self, text: str) -> int:
        text = " ".join(text.split())[:300]
        with self._lock, self._db:
            row = self._db.execute("SELECT id FROM memories WHERE lower(text)=lower(?)", (text,)).fetchone()
            if row:
                return row[0]
            if self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] >= 50:
                raise ValueError("memory is full (50 items); forget something first")
            return self._db.execute("INSERT INTO memories (text,created) VALUES (?,?)", (text, time.time())).lastrowid

    def forget_memory(self, ref: Any) -> int:
        """Delete by id, or by (part of) the text. Returns how many were removed."""
        with self._lock, self._db:
            if str(ref).isdigit():
                return self._db.execute("DELETE FROM memories WHERE id=?", (int(ref),)).rowcount
            return self._db.execute("DELETE FROM memories WHERE lower(text) LIKE ?", (f"%{str(ref).lower()}%",)).rowcount

    # ---- feedback (kept locally too, so a thumb is never lost if the Auditor is down)
    def save_feedback(self, turn_id: str, cid: str, rating: str, comment: str | None) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,0,?)", (turn_id, cid, rating, (comment or "")[:1000], time.time()))

    def mark_feedback_sent(self, turn_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE feedback SET sent=1 WHERE turn_id=?", (turn_id,))

    def feedback_for(self, cid: str) -> dict[str, str]:
        with self._lock:
            rows = self._db.execute("SELECT turn_id,rating FROM feedback WHERE conversation_id=?", (cid,)).fetchall()
        return dict(rows)

    # ---- search across conversations
    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        q = query.strip().lower()
        if len(q) < 2:
            return []
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            titles = self._db.execute("SELECT id,title,updated FROM conversations WHERE lower(title) LIKE ? ORDER BY updated DESC LIMIT ?", (f"%{q}%", limit)).fetchall()
            msgs = self._db.execute(
                "SELECT m.conversation_id,c.title,c.updated,m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id "
                "WHERE lower(m.content) LIKE ? ORDER BY m.n DESC LIMIT 300", (f"%{json.dumps(q)[1:-1]}%",)).fetchall()
        for cid, title, updated in titles:
            out[cid] = {"id": cid, "title": title, "updated": updated, "snippet": ""}
        for cid, title, updated, raw in msgs:
            if cid in out and out[cid]["snippet"]:
                continue
            content = json.loads(raw)
            text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            i = text.lower().find(q)
            if i < 0:
                continue
            a = max(0, i - 40)
            out[cid] = {"id": cid, "title": title, "updated": updated, "snippet": ("…" if a else "") + " ".join(text[a:i + 80].split())}
        return sorted(out.values(), key=lambda r: -r["updated"])[:limit]

    # ---- which model answered which turn (so thumbs survive a reload)
    def add_turn(self, cid: str, turn_id: str, model: str, provider: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT INTO turns (conversation_id,turn_id,model,provider) VALUES (?,?,?,?)", (cid, turn_id, model, provider))

    def drop_turn(self, turn_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM turns WHERE turn_id=?", (turn_id,))

    def turns(self, cid: str) -> list[dict[str, str]]:
        with self._lock:
            rows = self._db.execute("SELECT turn_id,model,provider FROM turns WHERE conversation_id=? ORDER BY n", (cid,)).fetchall()
        return [{"turn_id": r[0], "model": r[1], "provider": r[2]} for r in rows]

    def keep_turns(self, cid: str, count: int) -> None:
        """Forget turn records after the first `count` (used when editing an earlier message or regenerating)."""
        for t in self.turns(cid)[count:]:
            self.drop_turn(t["turn_id"])
