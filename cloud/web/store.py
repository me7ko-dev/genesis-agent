"""Данните на уеб услугата в един SQLite файл: хора, сесии, разговори, ходове.

Паролите са scrypt със сол; сесиите се пазят като SHA-256 на жетона, тоест
изтекла база не дава вход. Всяка операция отваря своя връзка — сървърът е
многонишков, а при този мащаб (бета с 10 души) това е по-просто от пул.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SESSION_SECONDS = 14 * 24 * 3600
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    chat_id TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued | running | ok | failed
    error TEXT NOT NULL DEFAULT '',
    tokens TEXT NOT NULL DEFAULT '{}',
    seconds REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    turn_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (turn_id, seq)
);
"""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, salt, digest = stored.split("$")
    except ValueError:
        return False
    if kind != "scrypt":
        return False
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    return hmac.compare_digest(got.hex(), digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()

    # ── хора и сесии ────────────────────────────────────────────────────────
    def add_user(self, email: str, password: str) -> int:
        email = email.strip().lower()
        if "@" not in email or len(password) < 10:
            raise ValueError("нужни са имейл и парола от поне 10 знака")
        try:
            with self._db() as c:
                cur = c.execute("INSERT INTO users (email, password, created) VALUES (?,?,?)",
                                (email, hash_password(password), time.time()))
        except sqlite3.IntegrityError:
            raise ValueError(f"{email} вече съществува") from None
        return int(cur.lastrowid or 0)

    def check_password(self, email: str, password: str) -> int | None:
        with self._db() as c:
            row = c.execute("SELECT id, password FROM users WHERE email = ?",
                            (email.strip().lower(),)).fetchone()
        if row is None:
            # Същото време като при грешна парола — да не издава кои имейли има.
            verify_password(password, hash_password("x" * 10))
            return None
        return int(row["id"]) if verify_password(password, row["password"]) else None

    def new_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._db() as c:
            c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            c.execute("INSERT INTO sessions VALUES (?,?,?)",
                      (_token_hash(token), user_id, now + SESSION_SECONDS))
        return token

    def session_user(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self._db() as c:
            row = c.execute(
                "SELECT u.id, u.email FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = ? AND s.expires > ?", (_token_hash(token), time.time()),
            ).fetchone()
        return {"id": int(row["id"]), "email": row["email"]} if row else None

    def end_session(self, token: str) -> None:
        with self._db() as c:
            c.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    # ── разговори и ходове ─────────────────────────────────────────────────
    def new_chat(self, user_id: int, title: str) -> str:
        chat_id = uuid.uuid4().hex
        with self._db() as c:
            c.execute("INSERT INTO chats VALUES (?,?,?,?)",
                      (chat_id, user_id, title[:80], time.time()))
        return chat_id

    def chats(self, user_id: int) -> list[dict[str, Any]]:
        with self._db() as c:
            rows = c.execute("SELECT id, title, created FROM chats WHERE user_id = ? "
                             "ORDER BY created DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def chat(self, user_id: int, chat_id: str) -> dict[str, Any] | None:
        """Разговорът, само ако е на този човек — чуждият изглежда като липсващ."""
        with self._db() as c:
            row = c.execute("SELECT id, title, created FROM chats WHERE id = ? AND user_id = ?",
                            (chat_id, user_id)).fetchone()
        return dict(row) if row else None

    def add_turn(self, chat_id: str, text: str) -> int:
        with self._db() as c:
            cur = c.execute("INSERT INTO turns (chat_id, text, status, created) "
                            "VALUES (?,?,'queued',?)", (chat_id, text, time.time()))
        return int(cur.lastrowid or 0)

    def set_status(self, turn_id: int, status: str) -> None:
        with self._db() as c:
            c.execute("UPDATE turns SET status = ? WHERE id = ?", (status, turn_id))

    def add_event(self, turn_id: int, event: dict[str, Any]) -> None:
        with self._db() as c:
            c.execute("INSERT INTO events (turn_id, seq, data) VALUES "
                      "(?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE turn_id = ?), ?)",
                      (turn_id, turn_id, json.dumps(event, ensure_ascii=False)))

    def finish_turn(self, turn_id: int, *, ok: bool, error: str,
                    tokens: dict[str, int], seconds: float) -> None:
        with self._db() as c:
            c.execute("UPDATE turns SET status = ?, error = ?, tokens = ?, seconds = ? WHERE id = ?",
                      ("ok" if ok else "failed", error[:500], json.dumps(tokens), seconds, turn_id))

    def turn(self, user_id: int, turn_id: int) -> dict[str, Any] | None:
        with self._db() as c:
            row = c.execute("SELECT t.* FROM turns t JOIN chats ch ON ch.id = t.chat_id "
                            "WHERE t.id = ? AND ch.user_id = ?", (turn_id, user_id)).fetchone()
        return self._turn_dict(row) if row else None

    def turns(self, chat_id: str) -> list[dict[str, Any]]:
        with self._db() as c:
            rows = c.execute("SELECT * FROM turns WHERE chat_id = ? ORDER BY id",
                             (chat_id,)).fetchall()
        return [self._turn_dict(r) for r in rows]

    def events(self, turn_id: int, after: int = 0) -> list[dict[str, Any]]:
        with self._db() as c:
            rows = c.execute("SELECT seq, data FROM events WHERE turn_id = ? AND seq > ? "
                             "ORDER BY seq", (turn_id, after)).fetchall()
        return [{"seq": r["seq"], **json.loads(r["data"])} for r in rows]

    def busy(self, *, user_id: int | None = None, chat_id: str | None = None) -> int:
        """Незавършени ходове (на човек, на разговор или общо)."""
        sql = ("SELECT COUNT(*) FROM turns t JOIN chats ch ON ch.id = t.chat_id "
               "WHERE t.status IN ('queued', 'running')")
        args: list[Any] = []
        if user_id is not None:
            sql += " AND ch.user_id = ?"
            args.append(user_id)
        if chat_id is not None:
            sql += " AND t.chat_id = ?"
            args.append(chat_id)
        with self._db() as c:
            return int(c.execute(sql, args).fetchone()[0])

    def fail_unfinished(self, reason: str) -> int:
        """При старт: ходовете, прекъснати от спиране на сървъра, не висят вечно."""
        with self._db() as c:
            cur = c.execute("UPDATE turns SET status = 'failed', error = ? "
                            "WHERE status IN ('queued', 'running')", (reason,))
        return cur.rowcount

    @staticmethod
    def _turn_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["tokens"] = json.loads(d.get("tokens") or "{}")
        return d
