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
