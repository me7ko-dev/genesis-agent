"""Отворените „ниски“ от NEXT_STEPS (2026-11-10), всяка с репродукция.

1. conversation_memory: реплика, завършваща на „|“, се слепваше със следващата
   в резюмето (2 искания → 1), а „cat x | user: y“ ставаше две.
2. conversation_memory: два писача компресираха повече — и двата броят 51,
   вторият свиваше вече свитото (30 сурови → 10; на два процеса × 60 реплики
   6 от 20 пускания губеха реплики, веднъж 21).
3. mcp_oauth: подновяване, срязано от срока на извикването, губеше завъртения
   refresh токен — сървърът вече е невалидирал стария, следва нов вход.
4. episodic_memory: `RUN_CMD mysql -phunter2 …` лежеше сурово в episodes.db.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from genesis_agent import conversation_memory as cm
from genesis_agent import episodic_memory as em
from genesis_agent import mcp_oauth

# ─── 1. „|“ в репликата ─────────────────────────────────────────────────────


def _items(messages: list[dict[str, str]]) -> list[tuple[str, str]]:
    parsed = cm._parse_summary({"role": "system", "content": cm._simple_summarize(messages)})
    assert parsed is not None
    return parsed[2]


def test_a_request_ending_in_a_pipe_does_not_swallow_the_next() -> None:
    items = _items([{"role": "user", "content": "pipe at end |"},
                    {"role": "user", "content": "второ искане"}])
    assert [t for _, t in items] == ["pipe at end ¦", "второ искане"]


def test_a_pipe_inside_a_request_does_not_split_it() -> None:
    assert _items([{"role": "user", "content": "cat x | user: y"}]) == [("user", "cat x ¦ user: y")]


def test_the_pipe_survives_a_second_compression() -> None:
    first = cm._simple_summarize([{"role": "user", "content": "a |"},
                                  {"role": "user", "content": "b"}])
    again = cm._simple_summarize([{"role": "system", "content": first},
                                  {"role": "user", "content": "c"}])
    assert again.endswith("user: a ¦ | user: b | user: c"), again


# ─── 2. Два писача ──────────────────────────────────────────────────────────


def test_two_writers_do_not_compress_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = cm._init_db()
    conn.executemany("INSERT INTO conversations (role, content) VALUES (?, ?);",
                     [("user", f"искане {i}") for i in range(51)])
    conn.commit()
    conn.close()

    counted, resume = threading.Event(), threading.Event()
    real_init = cm._init_db

    class Paused:
        """Връзката на първия писач: спира след броенето, докато вторият работи."""

        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            cur = self.conn.execute(sql, *args)
            if sql.startswith("SELECT COUNT"):
                counted.set()
                resume.wait(1.0)
            return cur

        def __getattr__(self, name: str) -> object:
            return getattr(self.conn, name)

    def init_db() -> object:
        conn = real_init()
        return Paused(conn) if threading.current_thread().name == "first" else conn

    monkeypatch.setattr(cm, "_init_db", init_db)

    def second() -> None:
        counted.wait(5)
        cm.summarize_old_context()
        resume.set()

    a = threading.Thread(target=cm.summarize_old_context, name="first")
    b = threading.Thread(target=second, name="second")
    a.start()
    b.start()
    a.join(15)
    b.join(15)

    conn = real_init()
    rows = conn.execute("SELECT role, content FROM conversations ORDER BY id;").fetchall()
    conn.close()
    raw = [c for r, c in rows if r != "system"]
    summaries = [c for r, c in rows if r == "system"]
    # Една компресия до keep=30: 30 сурови + едно резюме за 21 реплики.
    assert len(raw) == 30 and raw[0] == "искане 21", rows
    assert len(summaries) == 1 and summaries[0].startswith("[Context summary] 21 messages")


# ─── 3. Завъртян refresh токен след срока ───────────────────────────────────


def test_a_refresh_cut_by_the_callers_deadline_keeps_the_rotated_token(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    got = threading.Event()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            got.set()
            time.sleep(1.0)   # сървърът вече е завъртял токена; отговорът закъснява
            body = json.dumps({"access_token": "нов-достъп", "refresh_token": "завъртян",
                               "expires_in": 3600}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    url = base + "/mcp"
    try:
        with mcp_oauth._FileLock():
            mcp_oauth._save(url, {
                "access_token": "стар", "refresh_token": "първи", "expires_at": time.time() - 10,
                "lifetime": 3600, "client_id": "c", "token_endpoint": base + "/token",
                "resource": url, "issuer": base})

        assert mcp_oauth.refresh(url, stale="стар", timeout=0.3) is None   # срокът на викащия
        assert got.is_set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (mcp_oauth.saved(url) or {}).get(
                "refresh_token") != "завъртян":
            time.sleep(0.05)
        entry = mcp_oauth.saved(url) or {}
        assert entry.get("refresh_token") == "завъртян"
        assert mcp_oauth.access_token(url) == "нов-достъп"
    finally:
        httpd.shutdown()


# ─── 4. Тайни в епизодната памет ────────────────────────────────────────────


@pytest.mark.parametrize(("goal", "secret"), [
    ("RUN_CMD mysql -u root -phunter2 shop", "hunter2"),
    ("RUN_CMD sshpass -p hunter2 ssh deploy@x", "hunter2"),
    ("RUN_CMD export STRIPE_KEY=sk_live_abcdefghijklmnop12", "sk_live_abcdefghijklmnop12"),
    ("RUN_CMD git clone https://u:tok123secret@github.com/a/b", "tok123secret"),
])
def test_a_command_with_a_secret_is_masked_in_episodes_db(goal: str, secret: str) -> None:
    em.record_episode(goal=goal, outcome=f"rc=1: {goal}", skill_path="genesis_skills.bridge",
                      lessons_learned=[f"не пускай {goal}"], tags=["tool", "run_cmd"])
    with sqlite3.connect(em.DB_PATH) as conn:
        dump = json.dumps(conn.execute("SELECT * FROM episodes;").fetchall(), ensure_ascii=False)
    assert secret not in dump
    assert "RUN_CMD" in dump and "***" in dump
