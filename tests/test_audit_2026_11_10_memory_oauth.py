"""Одит, седми кръг (2026-11-10) — епизодната памет и OAuth на MCP.
Всеки тест пада без поправката си."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from genesis_agent import episodic_memory as em
from genesis_agent.notifier import redact

# ── episodes.db: пълният redact ядеше обикновени аргументи ────────────────────

@pytest.mark.parametrize("text", [
    "RUN_CMD mkdir -p build/out && cd build",
    "RUN_CMD pytest -q -p no:cacheprovider tests/test_a.py",
    "RUN_CMD git push -u origin feature/x",
    "RUN_CMD python -u train.py --epochs 3",
    "RUN_CMD docker run -p 8080:80 nginx",
    "RUN_CMD sort -u names.txt",
    "SEARCH_CODE primary key=id",
    "Сортирай с sorted(rows, key=lambda r: r.id), не с cmp",
])
def test_ordinary_commands_are_remembered_whole(text: str) -> None:
    em.record_episode(goal=text, outcome=f"rc=0: {text}", skill_path="genesis_skills.bridge",
                      lessons_learned=[text], tags=["tool"])
    with sqlite3.connect(em.DB_PATH) as conn:
        goal, outcome, lessons = conn.execute(
            "SELECT goal, outcome, lessons_learned FROM episodes;").fetchone()
    assert goal == text and outcome == f"rc=0: {text}"
    assert json.loads(lessons) == [text]


def test_notifications_keep_the_greedy_rules() -> None:
    assert redact("deploy -p hunter2") == "deploy -p ***"
    assert redact("deploy -p hunter2", memory=True) == "deploy -p hunter2"
    assert "hunter2" not in redact("mysql -u root -phunter2 shop", memory=True)
    assert "hunter2" not in redact("tool --password hunter2", memory=True)


# ── OAuth: процесът излиза веднага след срока на викащия ──────────────────────

_CHILD = r'''
import sys, time
from genesis_agent import mcp_oauth
base = sys.argv[1]
url = base + "/mcp"
with mcp_oauth._FileLock():
    mcp_oauth._save(url, {"access_token": "old", "refresh_token": "first",
                          "expires_at": time.time() - 10, "lifetime": 3600, "client_id": "c",
                          "token_endpoint": base + "/token", "resource": url, "issuer": base})
print("refresh ->", mcp_oauth.refresh(url, stale="old", timeout=0.5))
'''


def test_a_rotated_token_survives_an_exit_right_after_the_deadline(tmp_path) -> None:
    """`genesis -p` или /exit: нишката daemon умираше с процеса, сървърът вече беше
    завъртял refresh токена, а на диска оставаше старият — следва нов /mcp login."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            time.sleep(1.0)       # токенът вече е завъртян; отговорът закъснява
            body = json.dumps({"access_token": "new", "refresh_token": "rotated",
                               "expires_in": 3600}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    home = tmp_path / "home" / ".genesis"
    env = {**os.environ, "GENESIS_HOME": str(home), "HOME": str(tmp_path / "home"),
           "USERPROFILE": str(tmp_path / "home"), "NO_PROXY": "127.0.0.1,localhost",
           "no_proxy": "127.0.0.1,localhost", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(Path(em.__file__).resolve().parent.parent)}
    try:
        out = subprocess.run(
            [sys.executable, str(script), f"http://127.0.0.1:{httpd.server_address[1]}"],
            capture_output=True, encoding="utf-8", env=env, timeout=60, check=False)
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert "refresh -> None" in out.stdout, out.stderr[-500:]
    store = json.loads((home / "mcp_tokens.json").read_text(encoding="utf-8"))
    assert next(iter(store.values()))["refresh_token"] == "rotated"
