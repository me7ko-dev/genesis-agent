"""Проверката на главния цикъл 2026-10-11 (A1–A12): ред на таговете, история
при грешка, доказателства за твърдения, ASK_USER, режим план, компактиране,
грешка на веригата, Stop в EXPLORE, ping-pong, code_check, TODO_WRITE.
Всеки тест пада без поправката си (пуснато срещу стария код)."""
from __future__ import annotations

import contextlib
import json
from collections import deque

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import hooks, sandbox


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    monkeypatch.setattr(gta, "WORKSPACE", root)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    gs._SEEN_PATHS.clear()
    hooks.workspace = root
    (root / "app.py").write_text("PRICE = 42\n", encoding="utf-8")
    yield root
    hooks.workspace = None
    gs._set_agent_scope(None)
    gs._set_cancel(None)


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _script(monkeypatch, replies):
    it = iter(replies)
    seen: list[list[dict]] = []

    def fake(messages, tools=None):
        seen.append([dict(m) for m in messages])
        r = next(it)
        if isinstance(r, BaseException):
            raise r
        return r
    monkeypatch.setattr(gta, "ask_genesis", fake)
    return seen


class _UI(gta.TurnUI):
    def __init__(self):
        self.log: list = []

    def thinking(self, label, spinner="dots"):
        return contextlib.nullcontext()

    def assistant(self, text): self.log.append(("assistant", text))
    def tool(self, name, result): self.log.append(("tool", name, result))
    def asked(self, q): self.log.append(("asked", q))
    def spinning(self, n): self.log.append(("spin", n))
    def warn(self, t): self.log.append(("warn", t))
    def info(self, t): self.log.append(("info", t))


def _fresh():
    return deque([{"role": "system", "content": "SYS"}], maxlen=gta._HISTORY_MAXLEN)


# ── A1: текстовите тагове се изпълняват в реда, в който са написани ─────────

def test_text_tags_run_in_written_order_backup_before_write(ws, monkeypatch):
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="allow"))
    gs.parse_and_execute_tools("[READ_FILE: app.py]")
    out = gs.parse_and_execute_tools(
        "Първо пазя копие:\n[RUN_CMD: cp app.py app.bak]\nПосле пиша новата версия:\n"
        "[WRITE_FILE: app.py]PRICE = 0\n[END_WRITE]")
    assert len(out) == 2
    assert (ws / "app.bak").read_text(encoding="utf-8") == "PRICE = 42\n"
    assert (ws / "app.py").read_text(encoding="utf-8").strip() == "PRICE = 0"


def test_text_read_before_edit_shows_old_content(ws):
    out = gs.parse_and_execute_tools(
        "[READ_FILE: app.py]\n[EDIT_FILE: app.py]PRICE = 42\n---GENESIS-REPLACE-WITH---\nPRICE = 7\n[END_EDIT]")
    assert len(out) == 2
    assert "PRICE = 42" in out[0] and "PRICE = 7" not in out[0]
    assert (ws / "app.py").read_text(encoding="utf-8") == "PRICE = 7\n"


# ── A5: нищо след ASK_USER в същия рунд не се изпълнява преди отговора ─────

def test_native_tools_after_ask_user_wait_for_the_answer(ws, monkeypatch):
    gs.parse_and_execute_tools("[READ_FILE: app.py]")
    _script(monkeypatch, [("", [
        _call("ASK_USER", {"question": "Да сменя ли цената на 0?"}, "a1"),
        _call("EDIT_FILE", {"path": "app.py", "old": "PRICE = 42", "new": "PRICE = 0"}, "e1"),
    ])])
    ui = _UI()
    out = gta.run_turn(_fresh(), "смени цената", ui)
    assert [e for e in ui.log if e[0] == "asked"]
    assert (ws / "app.py").read_text(encoding="utf-8") == "PRICE = 42\n"
    # Двойката извикване/резултат остава цяла — иначе следващата заявка е невалидна.
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["a1", "e1"]
    assert "чака отговора" in tool_msgs[1]["content"]


def test_text_tags_after_ask_user_wait_for_the_answer(ws, monkeypatch):
    reply = "[ASK_USER: Да презапиша ли notes.txt?]\n[WRITE_FILE: notes.txt]ново\n[END_WRITE]"
    _script(monkeypatch, [(reply, None)])
    ui = _UI()
    gta.run_turn(_fresh(), "x", ui)
    assert [e for e in ui.log if e[0] == "asked"]
    assert not (ws / "notes.txt").exists()


# ── A2: грешка посред хода не изтрива свършеното от живата история ─────────

def _write_then_crash(monkeypatch):
    return _script(monkeypatch, [
        ("", [_call("WRITE_FILE", {"path": "made.py", "content": "x = 1\n"}, "w1")]),
        RuntimeError("ReadTimeout от доставчика"),
        ("ок", None),
    ])


def test_an_error_mid_turn_hands_back_the_work_done(ws, monkeypatch):
    _write_then_crash(monkeypatch)
    messages = _fresh()
    # Точно както main(): `messages = run_turn(...)` в try / except Exception.
    try:
        messages = gta.run_turn(messages, "направи made.py", _UI())
    except Exception as e:
        assert "ReadTimeout" in str(e)
        messages = gta.history_after_error(e, messages)
    assert (ws / "made.py").exists()
    assert any(m["role"] == "tool" and "made.py" in str(m["content"]) for m in messages)
    assert getattr(messages, "maxlen", None) == gta._HISTORY_MAXLEN


def test_the_phone_keeps_the_work_of_a_crashed_turn(ws, monkeypatch):
    import time

    from genesis_agent import remote_server as rs
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))
    seen = _write_then_crash(monkeypatch)
    monkeypatch.setattr(rs, "_print_qr", lambda *a: None)
    cap: dict = {}
    real_init = rs.RemoteServer.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        cap["server"] = self

    def wait(session):
        deadline = time.time() + 15
        time.sleep(0.05)
        while session.busy and time.time() < deadline:
            time.sleep(0.02)

    class FakeHTTP:
        def serve_forever(self, poll_interval=0.5):
            srv = cap["server"]
            srv.session.send("направи made.py")
            wait(srv.session)
            srv.session.send("какво направи?")
            wait(srv.session)
            raise KeyboardInterrupt

        def server_close(self):
            pass
    monkeypatch.setattr(rs.RemoteServer, "__init__", init)
    monkeypatch.setattr(rs.RemoteServer, "make_http", lambda self, h, p: FakeHTTP())
    assert rs.serve(["--port", "0"]) == 0
    assert len(seen) == 3
    assert any(m.get("role") == "tool" and "made.py" in str(m.get("content")) for m in seen[2])
