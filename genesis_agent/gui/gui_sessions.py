"""
genesis_agent.gui.gui_sessions — persisted chat sessions for the GTK app's
Recents sidebar.

Nothing else in the project stores a chat as a resumable, titled unit:
`conversation_memory.py` is one global flat table with no session boundary
(and it destructively summarizes old rows away), and `workspace_memory.py` is
task/decision memory, not transcripts. This is deliberately its own small
SQLite store, GUI-only — `agent_core.Core`/`run_tool_loop` stay
session-agnostic, exactly as before this module existed.

Same conventions as workspace_memory.py: one DB under DATA_DIR, WAL mode,
schema created on every connect (idempotent), UTC ISO timestamps.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from genesis_agent.config import DATA_DIR

DB_PATH = DATA_DIR / "gui_sessions.db"

_TITLE_LEN = 48

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    messages_json TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(_SCHEMA)
    return conn


def new_id() -> str:
    return uuid.uuid4().hex


def _derive_title(messages: list[dict[str, Any]]) -> str:
    for m in messages:
        if m.get("role") == "user":
            text = str(m.get("content") or "").strip()
            if text:
                text = " ".join(text.split())  # collapse newlines/whitespace
                return text[:_TITLE_LEN] + ("…" if len(text) > _TITLE_LEN else "")
    return "Нов разговор"


def save(session_id: str, messages: list[dict[str, Any]]) -> None:
    """Insert or update a session. Only messages with role user/assistant/tool
    are worth persisting — the system prompt is regenerated fresh on load, not
    stored (it can be large and is identical across every session anyway)."""
    keep = [m for m in messages if m.get("role") != "system"]
    if not keep:
        return
    title = _derive_title(keep)
    payload = json.dumps(keep, ensure_ascii=False)
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at, messages_json)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "   title = excluded.title,"
            "   updated_at = excluded.updated_at,"
            "   messages_json = excluded.messages_json;",
            (session_id, title, now, now, payload),
        )


def list_recent(limit: int = 30) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, title, updated_at FROM sessions"
            " ORDER BY updated_at DESC LIMIT ?;",
            (limit,),
        ).fetchall()
    return [{"id": r[0], "title": r[1], "updated_at": r[2]} for r in rows]


def load(session_id: str) -> list[dict[str, Any]] | None:
    with _conn() as c:
        row = c.execute(
            "SELECT messages_json FROM sessions WHERE id = ?;", (session_id,)
        ).fetchone()
    if not row:
        return None
    result: list[dict[str, Any]] = json.loads(row[0])
    return result


def delete(session_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM sessions WHERE id = ?;", (session_id,))
