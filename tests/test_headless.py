"""`genesis -p` — one turn without the chat, the answer alone on stdout."""
from __future__ import annotations

import io
import json

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import headless, plan_mode, sandbox


@pytest.fixture
def ws(tmp_path, monkeypatch):
    # run() сменя тези глобални — monkeypatch ги връща след теста
    monkeypatch.setattr(gta, "console", gta.console)
    monkeypatch.setattr(gta, "WORKSPACE", gta.WORKSPACE)
    monkeypatch.setattr(gs, "_WORKSPACE", gs._WORKSPACE)
    monkeypatch.setattr(sandbox, "_POLICY", sandbox._POLICY)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return tmp_path


def _script(monkeypatch, replies):
    it = iter(replies)
    seen: list[list[dict]] = []

    def fake(messages, tools=None):
        seen.append(list(messages))
        print("[Brain] noise that must not reach stdout")
        return next(it)
    monkeypatch.setattr(gta, "ask_genesis", fake)
    return seen


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_only_the_answer_reaches_stdout(ws, monkeypatch, capsys) -> None:
    _script(monkeypatch, [("app.py печата hi.", None)])
    assert headless.run(["какво", "прави", "app.py?", "--cwd", str(ws)]) == 0
    out, err = capsys.readouterr()
    assert out == "app.py печата hi.\n"
    assert "noise" in err


def test_json_reports_tools_and_runs_in_cwd(ws, monkeypatch, capsys) -> None:
    _script(monkeypatch, [("", [_call("READ_FILE", {"path": "app.py"})]), ("Печата hi.", None)])
    assert headless.run(["обясни", "--json", "--cwd", str(ws)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] and data["result"] == "Печата hi." and data["tools"] == ["READ_FILE"]
    assert data["workspace"] == str(ws.resolve())


def test_risky_actions_are_refused_without_a_person(ws, monkeypatch, capsys) -> None:
    seen = _script(monkeypatch, [("", [_call("RUN_CMD", {"command": "rm -rf build"})]), ("Не можах.", None)])
    headless.run(["изчисти", "--cwd", str(ws)])
    tool_results = [m["content"] for m in seen[-1] if m.get("role") == "tool"]
    assert any("DENIED" in r for r in tool_results)


def test_plan_mode_changes_nothing(ws, monkeypatch, capsys) -> None:
    _script(monkeypatch, [("", [_call("WRITE_FILE", {"path": "new.py", "content": "x"})]), ("План: …", None)])
    try:
        headless.run(["добави", "new.py", "--plan", "--cwd", str(ws)])
    finally:
        plan_mode.set_active(False)
    assert not (ws / "new.py").exists()


def test_piped_stdin_is_part_of_the_task(ws, monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("Traceback: ZeroDivisionError"))
    seen = _script(monkeypatch, [("Делиш на нула.", None)])
    headless.run(["обясни", "грешката", "--cwd", str(ws)])
    user = next(m["content"] for m in seen[0] if m.get("role") == "user")
    assert "обясни грешката" in user and "ZeroDivisionError" in user


def test_no_model_is_a_failure(ws, monkeypatch, capsys) -> None:
    _script(monkeypatch, [("[Грешка: цялата верига е изчерпана]", None)])
    assert headless.run(["здравей", "--json", "--cwd", str(ws)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_usage(ws, capsys) -> None:
    assert headless.run([]) == 2
    assert headless.run(["x", "--cwd", str(ws / "missing")]) == 2


# ── паралелни четения в един рунд ───────────────────────────────────────────

def test_independent_reads_run_at_once_and_keep_their_order(monkeypatch) -> None:
    import time
    calls = [_call("READ_FILE", {"path": f"f{i}.py"}, f"c{i}") for i in range(4)]

    def slow(name, args):
        time.sleep(0.4)
        return f"{name}:{args['path']}"
    monkeypatch.setattr(gs, "dispatch_tool_call", slow)
    started = time.monotonic()
    out = gta._parallel_reads(calls)
    assert time.monotonic() - started < 1.2
    assert [out[i] for i in range(4)] == [f"READ_FILE:f{i}.py" for i in range(4)]


def test_a_batch_with_a_write_stays_sequential(monkeypatch) -> None:
    calls = [_call("READ_FILE", {"path": "a.py"}), _call("WRITE_FILE", {"path": "b.py", "content": ""}, "c2")]
    assert gta._parallel_reads(calls) == {}
    assert gta._parallel_reads([_call("READ_FILE", {"path": "a.py"})]) == {}


# ── одит 2026-10-09 ─────────────────────────────────────────────────────────

def test_a_turn_cut_by_the_round_cap_is_not_a_success(ws, monkeypatch, capsys) -> None:
    monkeypatch.setattr(gta, "_TOOL_ROUND_CAP", 2)
    _script(monkeypatch, [("Ще прочета app.py.", [_call("READ_FILE", {"path": "app.py"}, f"c{i}")])
                          for i in range(5)])
    code = headless.run(["оправи", "--json", "--cwd", str(ws)])
    data = json.loads(capsys.readouterr().out)
    assert code == 1 and data["ok"] is False and "таван" in data["error"]


def test_a_question_has_its_own_exit_code(ws, monkeypatch, capsys) -> None:
    _script(monkeypatch, [("", [_call("ASK_USER", {"question": "Кой файл?"})])])
    code = headless.run(["оправи", "--json", "--cwd", str(ws)])
    data = json.loads(capsys.readouterr().out)
    assert code == 3 and "Кой файл?" in data["question"]


def test_an_error_in_the_turn_still_gives_json(ws, monkeypatch, capsys) -> None:
    def boom(messages, tools=None):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(gta, "ask_genesis", boom)
    assert headless.run(["x", "--json", "--cwd", str(ws)]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False and "provider exploded" in data["error"]


@pytest.mark.parametrize("stdin", [None, "bytes"])
def test_closed_or_odd_stdin_does_not_crash(ws, monkeypatch, capsys, stdin) -> None:
    if stdin is None:
        monkeypatch.setattr("sys.stdin", None)
    else:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"log \xff\xfe end")))
    seen = _script(monkeypatch, [("ok", None)])
    assert headless.run(["обясни", "--cwd", str(ws)]) == 0
    if stdin:
        user = next(m["content"] for m in seen[0] if m.get("role") == "user")
        assert "log" in user and "end" in user


def test_the_chat_state_is_left_as_it_was(ws, monkeypatch, capsys) -> None:
    remembered: list[str] = []

    class Mem:
        def add_message(self, role, content):
            remembered.append(content)
    monkeypatch.setattr(gta, "_conv_mem", Mem())
    before = (gta.console, sandbox.get_policy(), gta.WORKSPACE)
    _script(monkeypatch, [("ok", None)])
    headless.run(["x", "--plan", "--cwd", str(ws)])
    assert (gta.console, sandbox.get_policy(), gta.WORKSPACE) == before
    assert plan_mode.active() is False and gta.PERSIST_HISTORY is True
    assert remembered == []


def test_reads_that_may_ask_stay_sequential(ws, monkeypatch) -> None:
    monkeypatch.setattr(gs, "_WORKSPACE", ws)
    (ws / ".env").write_text("KEY=1", encoding="utf-8")
    safe = [_call("READ_FILE", {"path": "app.py"}), _call("READ_FILE", {"path": "app.py"}, "c2")]
    monkeypatch.setattr(gs, "dispatch_tool_call", lambda n, a: "ok")
    assert gta._parallel_reads(safe) != {}
    risky = [_call("READ_FILE", {"path": ".env"}), _call("READ_FILE", {"path": "app.py"}, "c2")]
    assert gta._parallel_reads(risky) == {}
    outside = [_call("READ_FILE", {"path": "/etc/hostname"}), _call("GLOB", {"pattern": "*"}, "c2")]
    assert gta._parallel_reads(outside) == {}
