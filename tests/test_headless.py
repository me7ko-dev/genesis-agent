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
    import threading
    calls = [_call("READ_FILE", {"path": f"f{i}.py"}, f"c{i}") for i in range(4)]
    # Бариера вместо хронометър (2026-10-15): на Windows CI 4 × 0.4 s
    # паралелно излязоха 1.3 s срещу таван 1.2. Четирите стигат бариерата само
    # ако вървят едновременно — последователно първото чака и гърми.
    together = threading.Barrier(4, timeout=10)

    def slow(name, args):
        together.wait()
        return f"{name}:{args['path']}"
    monkeypatch.setattr(gs, "dispatch_tool_call", slow)
    out = gta._parallel_reads(calls)
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


# ── --output-format json / stream-json (2026-10-15) ─────────────────────────
# Като `claude -p --output-format json`: един обект, по който скрипт решава —
# кои модели, колко токена (както доставчикът ги е отчел), рундове, файлове.

def _script_with_usage(monkeypatch, replies, usage):
    """Като _script, но всяка реплика отчита usage през budget.record_usage —
    същия път, по който минава Brain._log_usage."""
    from genesis_agent import budget
    it = iter(replies)

    def fake(messages, tools=None):
        provider, model, prompt, completion, extra = next(usage)
        budget.record_usage(provider=provider, model=model, prompt_tokens=prompt,
                            completion_tokens=completion, **extra)
        return next(it)
    monkeypatch.setattr(gta, "ask_genesis", fake)


def test_json_reports_models_tokens_rounds_and_files(ws, monkeypatch, capsys) -> None:
    usage = iter([("groq", "gpt-oss-120b", 1000, 50, {"cached_read_tokens": 400}),
                  ("nvidia", "nemotron", 2000, 80, {}),
                  ("groq", "gpt-oss-120b", 1500, 20, {"cached_read_tokens": 900}),
                  ("groq", "gpt-oss-120b", 500, 10, {})])
    done = "Готово: new.py и app.py."
    # Последната реплика е отговорът на подканата „пробвай кода“ (code_check).
    _script_with_usage(monkeypatch, [
        ("", [_call("WRITE_FILE", {"path": "new.py", "content": "x = 1\n"}),
              _call("READ_FILE", {"path": "app.py"}, "c2")]),
        ("", [_call("WRITE_FILE", {"path": "app.py", "content": "print('bye')\n"}, "c3")]),
        (done, None), (done, None)], usage)
    assert headless.run(["направи", "--output-format", "json", "--cwd", str(ws)]) == 0
    data = json.loads(capsys.readouterr().out)   # целият stdout е един обект — `| jq` работи
    assert data["type"] == "result" and data["is_error"] is False and data["exit_code"] == 0
    assert data["result"] == done and data["errors"] == []
    assert data["rounds"] == 4
    assert data["usage"] == {"input_tokens": 5000, "output_tokens": 160, "cached_read_tokens": 1300,
                             "cached_write_tokens": 0, "estimated": False}
    assert data["models"] == [
        {"provider": "groq", "model": "gpt-oss-120b", "calls": 3, "input_tokens": 3000, "output_tokens": 80},
        {"provider": "nvidia", "model": "nemotron", "calls": 1, "input_tokens": 2000, "output_tokens": 80}]
    assert data["cost_usd"] is None              # никой от тях не казва цена
    assert data["files"] == {"created": ["new.py"], "edited": ["app.py"], "deleted": []}
    assert isinstance(data["duration_ms"], int)
    assert data["tools"] == ["WRITE_FILE", "READ_FILE", "WRITE_FILE"]


def test_json_cost_is_the_sum_the_provider_reported(ws, monkeypatch, capsys) -> None:
    usage = iter([("claude_cli", "sonnet", 100, 5, {"cost_usd": 0.0125}),
                  ("claude_cli", "sonnet", 120, 7, {"cost_usd": 0.0075})])
    _script_with_usage(monkeypatch, [("", [_call("READ_FILE", {"path": "app.py"})]), ("hi", None)], usage)
    assert headless.run(["x", "--json", "--cwd", str(ws)]) == 0
    assert json.loads(capsys.readouterr().out)["cost_usd"] == 0.02


def test_json_without_provider_usage_says_the_tokens_are_estimated(ws, monkeypatch, capsys) -> None:
    def fake(messages, tools=None):
        gta.count_usage(None, messages, "отговор " * 40)   # както ask_genesis без usage
        return "отговор " * 40, None
    monkeypatch.setattr(gta, "ask_genesis", fake)
    monkeypatch.setattr(gta, "total_input_tokens", 0)
    monkeypatch.setattr(gta, "total_output_tokens", 0)
    monkeypatch.setattr(gta, "last_context_tokens", 0)
    assert headless.run(["x", "--output-format=json", "--cwd", str(ws)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["usage"]["estimated"] is True and data["usage"]["output_tokens"] > 0
    assert data["models"] == [] and data["files"] == {"created": [], "edited": [], "deleted": []}


def test_json_files_are_only_this_turns(ws, monkeypatch, capsys) -> None:
    from genesis_agent import edit_history
    edit_history.begin_turn("по-стар ход")
    edit_history.record(ws / "old.py")
    (ws / "old.py").write_text("x", encoding="utf-8")
    edit_history.end_turn()
    _script(monkeypatch, [("Само прочетох.", None)])
    assert headless.run(["x", "--json", "--cwd", str(ws)]) == 0
    assert json.loads(capsys.readouterr().out)["files"]["created"] == []


def test_json_failure_is_an_error_with_the_reason(ws, monkeypatch, capsys) -> None:
    _script(monkeypatch, [("[Грешка: цялата верига е изчерпана]", None)])
    assert headless.run(["x", "--output-format", "json", "--cwd", str(ws)]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["is_error"] is True and data["exit_code"] == 1 and data["errors"]


def test_json_red_tests_are_reflected(ws, monkeypatch, capsys) -> None:
    red = "[RUN_CMD: python -m pytest -q]  (rc=1)\n..F\n1 failed, 2 passed"
    real = gs.dispatch_tool_call
    monkeypatch.setattr(gs, "dispatch_tool_call",
                        lambda name, args: red if name == "RUN_CMD" else real(name, args))
    claim = "Готово! Всички 3 теста минават."
    _script(monkeypatch, [("", [_call("RUN_CMD", {"command": "python -m pytest -q"}, "t1")]),
                          (claim, None), (claim, None), (claim, None)])
    code = headless.run(["оправи", "--output-format", "json", "--cwd", str(ws)])
    data = json.loads(capsys.readouterr().out)
    assert code == 1 and data["is_error"] is True and data["tests_red"] is True
    assert any("червено" in e for e in data["errors"])


def test_stream_json_is_one_json_object_per_line_and_ends_with_the_result(ws, monkeypatch, capsys) -> None:
    _script(monkeypatch, [("", [_call("READ_FILE", {"path": "app.py"})]), ("Печата hi.", None)])
    assert headless.run(["обясни", "--output-format", "stream-json", "--cwd", str(ws)]) == 0
    out, err = capsys.readouterr()
    events = [json.loads(line) for line in out.splitlines()]   # нито един ред не е друго
    assert events[0]["type"] == "system" and events[-1]["type"] == "result"
    assert any(e["type"] == "tool" and e["name"] == "READ_FILE" for e in events)
    assert events[-1]["result"] == "Печата hi." and "noise" in err


def test_a_wrong_output_format_is_bad_usage(ws, capsys) -> None:
    assert headless.run(["x", "--output-format", "xml", "--cwd", str(ws)]) == 2
    assert headless.run(["x", "--output-format"]) == 2
    assert "text | json | stream-json" in capsys.readouterr().err


def test_the_real_command_prints_nothing_but_the_object(tmp_path) -> None:
    """Целият `genesis -p … --output-format json` в подпроцес срещу фалшивия
    доставчик (tests/fake_openai_provider.py): stdout е само обектът — нито
    [Brain] редове, нито банер; моделът и токените са истинските от отговора."""
    import os
    import re
    import subprocess
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fake_openai_provider as fp

    proj = tmp_path / "proj"
    proj.mkdir()
    script = [[{"name": "WRITE_FILE", "arguments": {"path": "hello.py", "content": "print('hi')\n"}}]]
    secret = re.compile(r"(_API_KEY(_\d+)?|_TOKEN|^GOOGLE_CLOUD_PROJECT)$")
    with fp.FakeProvider(script, final="Написах hello.py.") as fake:
        env = {k: v for k, v in os.environ.items() if not secret.search(k) and not k.startswith("GENESIS_")}
        env.update(GENESIS_MODEL_GATEWAY=fake.url, GROQ_API_KEY="fake-key",
                   GENESIS_HOME=str(tmp_path / "home"), GENESIS_DATA_DIR=str(tmp_path / "data"),
                   GENESIS_LOCAL_MODEL="", GENESIS_NO_PROJECT_ENV="1", GENESIS_ACCEPTANCE="0",
                   XDG_DATA_HOME=str(tmp_path / "xdg"), PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        proc = subprocess.run([sys.executable, "-m", "genesis_agent.cli", "-p", "write hello.py",
                               "--output-format", "json", "--cwd", str(proj)],
                              cwd=str(Path(__file__).resolve().parent.parent), env=env,
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=240, check=False)
    data = json.loads(proc.stdout)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert data["ok"] and data["result"] == "Написах hello.py."
    assert data["files"]["created"] == ["hello.py"] and (proj / "hello.py").is_file()
    assert data["models"] and all(m["provider"] == "groq" for m in data["models"])
    assert data["usage"]["input_tokens"] == 100 * data["rounds"]   # 100 на заявка от фалшивия


def test_text_stays_the_default(ws, monkeypatch, capsys) -> None:
    _script(monkeypatch, [("app.py печата hi.", None)])
    assert headless.run(["x", "--output-format", "text", "--cwd", str(ws)]) == 0
    assert capsys.readouterr().out == "app.py печата hi.\n"


@pytest.mark.skipif(__import__("os").name == "nt", reason="име на файл извън UTF-8 е POSIX случай")
def test_json_survives_a_file_name_that_is_not_utf8(ws, monkeypatch) -> None:
    """Команда създаде файл с име извън UTF-8 (surrogateescape); json.dumps с
    ensure_ascii=False даваше ред, който строг UTF-8 stdout не може да изпише
    (UnicodeEncodeError), а CLI с errors=replace печаташе `?.txt` (одит
    2026-10-21). Escape-нат сурогат е валиден JSON."""
    import io
    import sys
    from pathlib import Path
    py = Path(sys.executable).as_posix()
    cmd = f'"{py}" -c "open(b\'\\xff.txt\',\'wb\').write(b\'1\')"'
    _script(monkeypatch, [("", [_call("RUN_CMD", {"command": cmd})]), ("Създадох файла.", None)])
    proj = ws / "proj"          # не самата tmp_path: там е и хранилището на снимките
    proj.mkdir()
    strict = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", strict)
    assert headless.run(["създай", "файл", "--output-format", "stream-json", "--cwd", str(proj)]) == 0
    strict.flush()
    lines = strict.buffer.getvalue().decode("utf-8").splitlines()
    result = json.loads(lines[-1])
    assert result["type"] == "result" and result["files"]["created"] == ["\udcff.txt"]
