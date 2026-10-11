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


# ── A3/A4: провалена команда не доказва свършена работа ───────────────────

def test_a_failed_install_does_not_prove_the_install():
    from genesis_agent import claim_check
    res = ("[RUN_CMD: pip install fastapi-nonexist]  (rc=1)\nstderr:\n"
           "ERROR: Could not find a version that satisfies the requirement fastapi-nonexist")
    entry = claim_check.counts_as_executed("RUN_CMD", "pip install fastapi-nonexist", res)
    claims = claim_check.unsupported_claims("Инсталирах пакета fastapi-nonexist.",
                                            [entry] if entry else [])
    assert [c.kind for c in claims] == ["инсталация"]


def test_a_failed_move_is_challenged_in_the_loop(ws, monkeypatch):
    seen = _script(monkeypatch, [("", [_call("RUN_CMD", {"command": "mv nosuch.jpg photos/"})]),
                                 ("Преместих снимките в photos/.", None),
                                 ("Не успях — nosuch.jpg го няма.", None)])
    monkeypatch.setattr(gs, "dispatch_tool_call", lambda name, args: (
        "[RUN_CMD: mv nosuch.jpg photos/]  (rc=1)\nstderr:\n"
        "mv: cannot stat 'nosuch.jpg': No such file or directory"))
    ui = _UI()
    gta.run_turn(_fresh(), "премести снимките", ui)
    assert any(e[0] == "warn" and "Твърди" in e[1] for e in ui.log)
    assert len(seen) == 3


def test_failing_tests_do_not_prove_that_the_tests_pass():
    from genesis_agent import claim_check
    res = "[RUN_CMD: pytest -q]  (rc=1)\nF.\nFAILED test_x.py::test_a - assert 1 == 2\n1 failed, 1 passed"
    entry = claim_check.counts_as_executed("RUN_CMD", "pytest -q", res)
    assert entry is not None
    # „Пуснах тестовете“ е вярно и при паднал тест …
    assert claim_check.unsupported_claims("Пуснах тестовете: test_a пада.", [entry]) == []
    # … „всички минаха“ — не.
    claims = claim_check.unsupported_claims("Готово — всички тестове минаха.", [entry])
    assert claims and "rc=1" in claim_check.nudge_text(claims)
    # Последното пускане решава: след поправка и зелено пускане твърдението е вярно.
    ok = claim_check.counts_as_executed("RUN_CMD", "pytest -q", "[RUN_CMD: pytest -q]  (rc=0)\n2 passed")
    assert ok is not None
    assert claim_check.unsupported_claims("Всички тестове минаха.", [entry, ok]) == []
    assert claim_check.unsupported_claims("All tests pass now.", [ok, entry])
    # Текстовият път дава същото.
    assert claim_check.unsupported_claims(
        "Тестовете минаха.", claim_check.executed_from_text_results([res]))


# ── A6: в режим план планът е отговорът, не „неизпълнено обещание“ ─────────

_PLAN = ("План:\n1. Ще създам utils.py с функцията parse_price().\n"
         "2. Ще напиша тестове в tests/test_utils.py.\n"
         "Проверка: pytest -q. Рискове: няма.\nСлед одобрение ще създам файловете.")


def _plan_replies():
    return [("", [_call("READ_FILE", {"path": "app.py"}, "r1")]),
            (_PLAN, None),
            ("", [_call("WRITE_FILE", {"path": "utils.py", "content": "x=1\n"}, "w1")]),
            ("Не мога да пиша в режим план.", None),
            ("край", None)]


def test_plan_mode_does_not_push_the_plan_into_execution(ws, monkeypatch):
    from genesis_agent import plan_mode
    plan_mode.set_active(True)
    try:
        seen = _script(monkeypatch, _plan_replies())
        ui = _UI()
        gta.run_turn(_fresh(), "добави parse_price", ui)
    finally:
        plan_mode.set_active(False)
    assert len(seen) == 2
    assert not [e for e in ui.log if e[0] == "warn"]


def test_headless_plan_prints_the_plan(ws, monkeypatch, capsys):
    from genesis_agent import headless
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))
    monkeypatch.setattr(headless, "_read_stdin", lambda: "")
    _script(monkeypatch, _plan_replies())
    code = headless.run(["--plan", "--json", "--cwd", str(ws), "добави parse_price"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and "parse_price" in out["result"]
    assert out["tools"] == ["READ_FILE"]


# ── A8: грешка на веригата не е отговор на модела ──────────────────────────

_CHAIN_ERR = "[Грешка: цялата верига е изчерпана | последна: groq 429]"


def test_a_chain_error_ends_the_turn_without_nudges(ws, monkeypatch):
    seen = _script(monkeypatch, [
        ("", [_call("WRITE_FILE", {"path": "calc.py",
                                   "content": "def add(a, b):\n    return a + b\n"}, "w1")]),
        (_CHAIN_ERR, None), (_CHAIN_ERR, None), (_CHAIN_ERR, None)])
    ui = _UI()
    msgs = gta.run_turn(_fresh(), "напиши calc.py с add()", ui)
    assert len(seen) == 2
    assert ("warn", _CHAIN_ERR) in ui.log
    assert not [m for m in msgs if m["role"] == "assistant"
                and str(m.get("content") or "").startswith("[Грешка")]
    # Свършеното преди грешката остава.
    assert any(m["role"] == "tool" and "calc.py" in str(m["content"]) for m in msgs)


def test_headless_reports_a_chain_error_as_the_error(ws, monkeypatch, capsys):
    from genesis_agent import headless
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))
    monkeypatch.setattr(headless, "_read_stdin", lambda: "")
    _script(monkeypatch, [(_CHAIN_ERR, None)])
    assert headless.run(["здравей", "--json", "--cwd", str(ws)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "верига" in (out["error"] or "")


# ── A7: повторното компактиране не реже предишното резюме ──────────────────

@pytest.mark.parametrize("ordinary_len", [10, 2000])
def test_recompaction_keeps_the_whole_previous_summary(monkeypatch, ordinary_len):
    from types import SimpleNamespace

    from genesis_agent import brain
    compact = brain.Brain.compact_chat_history
    seen: list[str] = []

    class Fake:
        def __init__(self, *a, **k):
            pass

        def complete(self, msgs, tools=None):
            seen.append(msgs[-1]["content"])
            return SimpleNamespace(raw_text="ново резюме")
    monkeypatch.setattr(brain, "Brain", Fake)
    old_summary = ("## Резюме на по-ранния разговор:\n" + "Работим по проекта faktura-excel. " * 15
                   + "РЕШЕНИЕ: базата е PostgreSQL 16, не SQLite.")
    msgs = [{"role": "system", "content": "SYS"}, {"role": "system", "content": old_summary}]
    for i in range(10):
        msgs += [{"role": "user", "content": f"въпрос {i} " + "x" * ordinary_len},
                 {"role": "assistant", "content": f"отговор {i}"}]
    out = compact(msgs, threshold=16, keep_recent=10)
    assert "PostgreSQL 16" in seen[0]
    assert len(seen[0]) <= 8000
    assert out[1]["content"].endswith("ново резюме")


# ── A9: „Стоп“ спира и EXPLORE ─────────────────────────────────────────────

def test_stop_reaches_explore(ws):
    from types import SimpleNamespace

    from genesis_agent import explore
    n = {"calls": 0}

    def complete(messages, tools=None):
        n["calls"] += 1
        return SimpleNamespace(raw_text="търся", tool_calls=[{"id": f"c{n['calls']}", "function": {
            "name": "SEARCH_CODE", "arguments": json.dumps({"pattern": "PRICE"})}}])
    gs._set_cancel(lambda: n["calls"] >= 1)   # операторът натиска „Стоп“ след първия рунд
    try:
        out = explore.explore("къде е цената?", str(ws), complete=complete)
    finally:
        gs._set_cancel(None)
    assert n["calls"] == 1
    assert "⏹ Спрян от оператора след 1 рунда" in out and "търся" in out
