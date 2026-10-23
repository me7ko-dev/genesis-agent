"""Одит на хода в терминала 2026-10-22: Ctrl-C в hook Stop и в компресията,
hook Stop след Ctrl-C и след грешка на веригата, Ctrl-C посред текстови тагове,
ASK_USER на екрана, Ctrl-C в паралелно четене и Σ след провалено обръщение.
Всеки тест пада без поправката си (пуснато срещу стария код)."""
from __future__ import annotations

import contextlib
import json
import threading
import time
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
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="allow"))
    gs._SEEN_PATHS.clear()
    hooks.workspace = root
    (root / "app.py").write_text("PRICE = 42\n", encoding="utf-8")
    yield root
    hooks.workspace = None
    gs._set_agent_scope(None)
    gs._set_cancel(None)


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


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


def _turn(*args):
    """run_turn, но изтекъл KeyboardInterrupt е провал на теста, не спрян pytest."""
    try:
        return gta.run_turn(*args)
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt избяга от run_turn")


def _no_hooks(event, data, *a, **k):
    return hooks.Outcome()


# ── 1. Ctrl-C в hook Stop или в компресията не губи свършения ход ───────────

@pytest.mark.parametrize("where", ["stop_hook", "compact"])
def test_ctrl_c_in_stop_hook_or_compaction_keeps_the_turn(ws, monkeypatch, where):
    _script(monkeypatch, [("", [_call("WRITE_FILE", {"path": "notes.txt", "content": "hello\n"}, "w1")]),
                          ("Записах notes.txt.", None)])
    if where == "stop_hook":
        def fire(event, data, *a, **k):
            if event == "Stop":
                raise KeyboardInterrupt     # операторът спира бавния `pytest -q` от hook-а
            return hooks.Outcome()
        monkeypatch.setattr(hooks, "fire", fire)
    else:
        monkeypatch.setattr(hooks, "fire", _no_hooks)

        def compact(messages):
            raise KeyboardInterrupt         # Ctrl-C, докато моделът за резюмето отговаря
        monkeypatch.setattr(gta, "_compact_messages", compact)
    msgs = _turn(_fresh(), "make notes.txt", _UI())   # не бива да хвърля
    assert (ws / "notes.txt").exists()
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "w1" for m in msgs)
    assert any(m.get("role") == "assistant" and "Записах" in str(m.get("content")) for m in msgs)


# ── 2. hook Stop не връща хода след Ctrl-C или след грешка на веригата ──────

@pytest.mark.parametrize("first", ["ctrl_c", "chain_error"])
def test_stop_hook_does_not_resume_after_ctrl_c_or_chain_error(ws, monkeypatch, first):
    monkeypatch.setattr(hooks, "fire", lambda event, data, *a, **k: (
        hooks.Outcome(blocked=True, message="run the tests first") if event == "Stop"
        else hooks.Outcome()))
    err = "[Грешка: цялата верига е изчерпана (429 навсякъде)]"
    head = KeyboardInterrupt() if first == "ctrl_c" else (err, None)
    seen = _script(monkeypatch, [head, ("още работа", None), ("пак", None)])
    msgs = _turn(_fresh(), "do it", _UI())
    assert len(seen) == 1
    assert not any("[hook Stop" in str(m.get("content")) for m in msgs)


# ── 3. Таг-режим: Ctrl-C посред рунда пази резултатите на изпълнените тагове ─

def test_ctrl_c_mid_text_round_keeps_finished_tag_results(ws, monkeypatch):
    monkeypatch.setattr(hooks, "fire", _no_hooks)

    def run_cmd(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setitem(gs._SIMPLE_DISPATCH, "RUN_CMD", run_cmd)
    reply = "Ще го направя.\n[WRITE_FILE: made.py]\nx = 1\n[END_WRITE]\n[RUN_CMD: python -m pytest -q]"
    _script(monkeypatch, [(reply, None)])
    ui = _UI()
    msgs = _turn(_fresh(), "make made.py and test", ui)
    assert (ws / "made.py").exists()
    results = [str(m.get("content")) for m in msgs
               if m.get("role") == "system" and str(m.get("content")).startswith("[Резултат]")]
    assert results, "резултатите от рунда липсват в историята"
    assert "made.py" in results[-1] and "RUN_CMD" in results[-1] and "прекъснато" in results[-1]
    assert any(e[0] == "tool" and e[1] == "WRITE_FILE" for e in ui.log)


# ── 4. ASK_USER — един панел, без вътрешния маркер ──────────────────────────

@pytest.mark.parametrize("native", [True, False])
def test_ask_user_is_shown_once_without_the_marker(ws, monkeypatch, native):
    from rich.console import Console
    monkeypatch.setattr(hooks, "fire", _no_hooks)
    rec = Console(record=True, width=100, force_terminal=False)
    monkeypatch.setattr(gta, "console", rec)
    first = (("", [_call("ASK_USER", {"question": "Which DB: sqlite or postgres?",
                                      "options": ["sqlite", "postgres"]}, "a1")]) if native
             else ("[ASK_USER: Which DB: sqlite or postgres?]", None))
    _script(monkeypatch, [first])
    _turn(_fresh(), "add a db", gta.RichTurnUI())
    out = rec.export_text()
    assert gs.ASK_USER_MARKER not in out
    assert out.count("❓ Which DB: sqlite or postgres?") == 1   # само панелът „Genesis пита“


# ── 5. Ctrl-C в паралелно четене: веднага, и готовите резултати остават ─────

def test_ctrl_c_in_parallel_reads_returns_at_once_and_keeps_done_results(ws, monkeypatch):
    monkeypatch.setattr(hooks, "fire", _no_hooks)
    for n in ("b.py", "c.py"):
        (ws / n).write_text("x = 1\n", encoding="utf-8")
    release = threading.Event()
    real = gs.dispatch_tool_call

    def dispatch(name, args):
        path = args.get("path", "")
        if path == "b.py":
            time.sleep(0.3)
            raise KeyboardInterrupt         # операторът натиска Ctrl-C посред партидата
        if path == "c.py":
            release.wait(10)                # бавен WEB_FETCH / SEARCH_CODE
        return real(name, args)
    monkeypatch.setattr(gs, "dispatch_tool_call", dispatch)
    _script(monkeypatch, [("", [_call("READ_FILE", {"path": "app.py"}, "r1"),
                                _call("READ_FILE", {"path": "b.py"}, "r2"),
                                _call("READ_FILE", {"path": "c.py"}, "r3")])])
    t0 = time.time()
    try:
        msgs = _turn(_fresh(), "read all", _UI())
        took = time.time() - t0
    finally:
        release.set()
    assert took < 5, f"Ctrl-C чака бавното четене ({took:.1f} s)"
    tools = {m["tool_call_id"]: str(m["content"]) for m in msgs if m.get("role") == "tool"}
    assert set(tools) == {"r1", "r2", "r3"}
    assert "PRICE = 42" in tools["r1"]
    assert "прекъснато" in tools["r3"]


def test_ctrl_c_in_parallel_reads_is_not_held_by_a_slow_read(ws, monkeypatch):
    """Ctrl-C, който не буди чакащата нишка (така е на Windows до 3.14):
    _thread.interrupt_main насрочва KeyboardInterrupt, без да прекъсва чакането.
    С as_completed без таймаут ходът стоеше до края на бавното четене."""
    import _thread
    monkeypatch.setattr(hooks, "fire", _no_hooks)
    (ws / "c.py").write_text("x = 1\n", encoding="utf-8")
    release = threading.Event()
    real = gs.dispatch_tool_call

    def dispatch(name, args):
        if args.get("path") == "c.py":
            time.sleep(0.3)                 # главната нишка вече чака партидата
            _thread.interrupt_main()
            release.wait(10)                # бавен WEB_FETCH / SEARCH_CODE
        return real(name, args)
    monkeypatch.setattr(gs, "dispatch_tool_call", dispatch)
    _script(monkeypatch, [("", [_call("READ_FILE", {"path": "app.py"}, "r1"),
                                _call("READ_FILE", {"path": "c.py"}, "r2")])])
    t0 = time.time()
    try:
        msgs = _turn(_fresh(), "read all", _UI())
        took = time.time() - t0
    finally:
        release.set()
    assert took < 5, f"Ctrl-C чака бавното четене ({took:.1f} s)"
    tools = {m["tool_call_id"]: str(m["content"]) for m in msgs if m.get("role") == "tool"}
    assert "PRICE = 42" in tools["r1"] and "прекъснато" in tools["r2"]


# ── 6. Провалено обръщение (изчерпана верига) не добавя токени към Σ ────────

def test_chain_error_adds_nothing_to_usage(monkeypatch):
    import genesis_agent.brain as gb

    class _Brain:
        current = None

        def __init__(self, *a, **k):
            pass

        def complete(self, messages, tools=None):
            return type("R", (), {"raw_text": "Error: all providers exhausted (429)",
                                  "usage": None, "tool_calls": None})()
    monkeypatch.setattr(gb, "Brain", _Brain)
    monkeypatch.setattr(gta, "_brain_handles", lambda p: True)
    monkeypatch.setattr(gta, "_CODING_MODE", True)    # без лекия маршрут
    gta.reset_usage()
    try:
        msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "x" * 40000}]
        text, calls = gta.ask_genesis(msgs, tools=[])
        assert text.startswith("[Грешка") and calls is None
        assert (gta.total_input_tokens, gta.total_output_tokens, gta.last_context_tokens) == (0, 0, 0)
    finally:
        gta.reset_usage()
