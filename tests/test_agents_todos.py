"""Собствени под-агенти (.genesis/agents/) и списъкът със задачи (TODO_WRITE)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import agents, chat_commands, hooks, paths, plan_mode, todos


class _Reply:
    def __init__(self, text: str = "", calls: list | None = None) -> None:
        self.raw_text = text
        self.tool_calls = calls


def _call(name: str, args: dict, cid: str = "c1") -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _script(*replies):
    it = iter(replies)
    seen: list[list[dict]] = []

    def complete(messages, tools=None):
        seen.append(list(messages))
        return next(it)
    complete.seen = seen  # type: ignore[attr-defined]
    complete.tools = []   # type: ignore[attr-defined]
    return complete


@pytest.fixture
def ws(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    monkeypatch.setattr(gta, "WORKSPACE", root)
    gs._SEEN_PATHS.clear()
    hooks.workspace = root
    (root / "app.py").write_text("PRICE = 42\n", encoding="utf-8")
    yield root
    hooks.workspace = None
    gs._set_agent_scope(None)


def _agent(folder: Path, name: str, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


READER = "---\nname: reader\ndescription: Чете кода и отговаря.\ntools: READ_FILE, SEARCH_CODE\n---\nТи си четец.\n"


# ── зареждане ──────────────────────────────────────────────────────────────

def test_project_agent_is_loaded_with_its_tools(ws) -> None:
    _agent(ws / ".genesis" / "agents", "reader", READER)
    found = agents.load(ws)
    assert list(found) == ["reader"]
    a = found["reader"]
    assert a.description == "Чете кода и отговаря." and a.prompt == "Ти си четец."
    assert a.tools == frozenset({"READ_FILE", "SEARCH_CODE"})


def test_claude_code_agents_work_unchanged(ws) -> None:
    _agent(ws / ".claude" / "agents", "rev", "---\nname: rev\ndescription: review\n"
           "tools: Read, Grep, Glob, Bash, Task, Frobnicate\nmodel: haiku\n---\nReview.\n")
    a = agents.load(ws)["rev"]
    assert a.tools == frozenset({"READ_FILE", "SEARCH_CODE", "GLOB", "RUN_CMD"})
    assert set(a.dropped) == {"Task", "Frobnicate"} and a.model == "haiku"


def test_project_wins_over_the_operators_file(ws) -> None:
    _agent(Path(paths.GENESIS_HOME) / "agents", "reader", "---\ndescription: оператор\n---\nx")
    _agent(Path(paths.GENESIS_HOME) / "agents", "mine", "---\ndescription: само мой\n---\ny")
    _agent(ws / ".genesis" / "agents", "reader", READER)
    found = agents.load(ws)
    assert found["reader"].description == "Чете кода и отговаря."
    assert found["mine"].description == "само мой"


def test_without_frontmatter_and_bad_names(ws) -> None:
    _agent(ws / ".genesis" / "agents", "plain", "Пише тестове за всичко.\nПодробности…")
    _agent(ws / ".genesis" / "agents", "bad", "---\nname: ../../x\n---\nзло")
    _agent(ws / ".genesis" / "agents", "broken", "---\nname: [unclosed\n---\nтекст")
    found = agents.load(ws)
    assert found["plain"].description == "Пише тестове за всичко." and found["plain"].tools is None
    assert "../../x" not in found and "bad" not in found
    assert "broken" not in found   # счупен YAML с невалидно име — пропуснат, не срив


# ── одит 2026-10-09 ────────────────────────────────────────────────────────

def test_broken_yaml_header_keeps_the_tool_limit(ws) -> None:
    _agent(ws / ".claude" / "agents", "rev", "---\nname: rev\ndescription: Use this agent when: "
           "the user finished a change. Examples: review PR\ntools: Read, Grep, Glob\n"
           "model: haiku\n---\nReview.\n")
    a = agents.load(ws)["rev"]
    assert a.tools == frozenset({"READ_FILE", "SEARCH_CODE", "GLOB"}) and a.model == "haiku"
    assert a.description.startswith("Use this agent when:")


@pytest.mark.skipif(not hasattr(__import__("os"), "symlink"), reason="symlinks")
def test_symlinked_agent_files_are_not_read(ws, tmp_path) -> None:
    import os
    secret = tmp_path / "outside" / ".env"
    secret.parent.mkdir()
    secret.write_text("ANTHROPIC_API_KEY=sk-ant-SECRET123\n", encoding="utf-8")
    folder = ws / ".genesis" / "agents"
    folder.mkdir(parents=True)
    try:
        os.symlink(secret, folder / "helper.md")
        os.symlink(Path(paths.GENESIS_HOME) / ".env", folder / "home.md")
    except OSError:
        pytest.skip("symlinks not allowed")
    (Path(paths.GENESIS_HOME) / ".env").write_text("KEY=sk-SECRET456\n", encoding="utf-8")
    assert agents.load(ws) == {}
    assert "SECRET" not in agents.prompt_section(ws)


@pytest.mark.skipif(not hasattr(__import__("os"), "mkfifo"), reason="POSIX")
def test_a_fifo_agent_file_does_not_block(ws) -> None:
    import os
    from concurrent.futures import ThreadPoolExecutor
    folder = ws / ".genesis" / "agents"
    folder.mkdir(parents=True)
    os.mkfifo(folder / "pipe.md")
    pool = ThreadPoolExecutor(1)
    try:
        assert pool.submit(agents.load, ws).result(timeout=5) == {}
    finally:
        pool.shutdown(wait=False)


def test_yaml_alias_bomb_does_not_hang(ws) -> None:
    import time
    levels = ["a: &a [x, x, x, x, x, x, x, x, x]"]
    for prev, cur in zip("abcdefg", "bcdefgh"):
        levels.append(f"{cur}: &{cur} [{', '.join(['*' + prev] * 9)}]")
    _agent(ws / ".genesis" / "agents", "bomb",
           "---\n" + "\n".join(levels) + "\nname: *h\ndescription: *h\ntools: *h\n---\nx")
    started = time.monotonic()
    found = agents.load(ws)
    agents.prompt_section(ws)
    assert time.monotonic() - started < 5
    assert found["bomb"].description == "x" and found["bomb"].tools == frozenset()


def test_repo_agents_cannot_crowd_out_the_operators(ws) -> None:
    for i in range(40):
        _agent(ws / ".genesis" / "agents", f"r{i:02d}", f"---\ndescription: {'д' * 250}\n---\nx")
    _agent(Path(paths.GENESIS_HOME) / "agents", "mine", "---\ndescription: моят\n---\ny")
    assert "mine" in agents.load(ws)
    assert len(agents.prompt_section(ws)) <= agents._MAX_SECTION + 200


def test_a_file_the_sub_agent_changed_must_be_read_again(ws) -> None:
    _agent(ws / ".genesis" / "agents", "fixer", "---\ndescription: оправя\ntools: READ_FILE, EDIT_FILE\n---\nx")
    gs.dispatch_tool_call("READ_FILE", {"path": "app.py"})
    fake = _script(_Reply(calls=[_call("READ_FILE", {"path": "app.py"}, "a")]),
                   _Reply(calls=[_call("EDIT_FILE", {"path": "app.py", "old": "42", "new": "43"}, "b")]),
                   _Reply("оправено"))
    agents.run("fixer", "оправи цената", ws, complete=fake)
    assert (ws / "app.py").read_text(encoding="utf-8") == "PRICE = 43\n"
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "app.py", "content": "PRICE = 42\nTAX = 1\n"})
    assert (ws / "app.py").read_text(encoding="utf-8") == "PRICE = 43\n", out


def test_ctrl_c_in_the_progress_line_does_not_leave_the_scope(ws, monkeypatch) -> None:
    _agent(ws / ".genesis" / "agents", "reader", READER)

    def interrupt(_text: str) -> None:
        raise KeyboardInterrupt
    monkeypatch.setattr(agents, "progress", interrupt)
    with pytest.raises(KeyboardInterrupt):
        agents.run("reader", "x", ws, complete=_script(_Reply("ок")))
    assert gs._agent_scope() is None


def test_an_empty_reply_is_reported_as_such(ws) -> None:
    _agent(ws / ".genesis" / "agents", "reader", READER)
    out = agents.run("reader", "x", ws, complete=_script(_Reply("")))
    assert "Празен отговор" in out and "1 рунда" in out


def test_semicolons_inside_a_step_do_not_split_it() -> None:
    items = todos.parse_text("[x] read app.py; [~] run `pytest -q; ruff check`; [ ] commit")
    assert [i["content"] for i in items] == ["read app.py", "run `pytest -q; ruff check`", "commit"]


def test_no_tools_means_all_but_nesting_and_the_main_conversation_state(ws) -> None:
    _agent(ws / ".genesis" / "agents", "all", "---\ndescription: всичко\n---\nx")
    allowed = agents.allowed_tools(agents.load(ws)["all"])
    assert {"READ_FILE", "WRITE_FILE", "RUN_CMD"} <= allowed
    assert not allowed & {"AGENT", "EXPLORE", "TODO_WRITE", "ASK_USER", "REMEMBER"}


# ── изпълнение ─────────────────────────────────────────────────────────────

def test_it_works_in_its_own_conversation_and_returns_the_report(ws) -> None:
    _agent(ws / ".genesis" / "agents", "reader", READER)
    fake = _script(_Reply(calls=[_call("READ_FILE", {"path": "app.py"})]),
                   _Reply("Цената е 42 (app.py:1)."))
    out = agents.run("reader", "колко е цената?", ws, complete=fake)
    assert out.startswith("[AGENT: reader]") and "Цената е 42" in out
    first = fake.seen[0]
    assert first[0]["role"] == "system" and "Ти си четец." in first[0]["content"]
    assert first[1] == {"role": "user", "content": "колко е цената?"}
    tool_msg = next(m for m in fake.seen[1] if m.get("role") == "tool")
    assert "PRICE = 42" in tool_msg["content"]


def test_tools_outside_its_list_are_refused_on_both_paths(ws) -> None:
    _agent(ws / ".genesis" / "agents", "reader", READER)
    fake = _script(_Reply(calls=[_call("WRITE_FILE", {"path": "x.py", "content": "boom"})]),
                   _Reply("[RUN_CMD: echo hacked > y.txt]"),
                   _Reply("Не можах."))
    agents.run("reader", "запиши", ws, complete=fake)
    assert not (ws / "x.py").exists() and not (ws / "y.txt").exists()
    native = next(m for m in fake.seen[1] if m.get("role") == "tool")["content"]
    text = fake.seen[2][-1]["content"]
    assert "няма този инструмент" in native and "няма този инструмент" in text


def test_scope_and_seen_files_are_restored_even_after_an_error(ws) -> None:
    _agent(ws / ".genesis" / "agents", "reader", READER)
    gs._SEEN_PATHS.add(ws / "main_saw.py")

    def boom(messages, tools=None):
        gs.dispatch_tool_call("READ_FILE", {"path": "app.py"})
        raise RuntimeError("мрежата падна")
    with pytest.raises(RuntimeError):
        agents.run("reader", "x", ws, complete=boom)
    assert gs._agent_scope() is None
    assert gs._SEEN_PATHS == {ws / "main_saw.py"}


def test_a_sub_agent_cannot_start_another(ws) -> None:
    _agent(ws / ".genesis" / "agents", "all", "---\ndescription: всичко\n---\nx")
    fake = _script(_Reply(calls=[_call("AGENT", {"agent": "all", "task": "пак"})]), _Reply("ок"))
    agents.run("all", "x", ws, complete=fake)
    assert "няма този инструмент" in next(m for m in fake.seen[1] if m.get("role") == "tool")["content"]


def test_plan_mode_still_refuses_its_writes(ws) -> None:
    _agent(ws / ".genesis" / "agents", "writer", "---\ndescription: пише\ntools: WRITE_FILE\n---\nx")
    plan_mode.set_active(True)
    fake = _script(_Reply(calls=[_call("WRITE_FILE", {"path": "n.py", "content": "1"})]), _Reply("ок"))
    agents.run("writer", "x", ws, complete=fake)
    assert not (ws / "n.py").exists()
    assert "Режим план" in next(m for m in fake.seen[1] if m.get("role") == "tool")["content"]


def test_its_writes_are_undoable_and_seen_by_hooks(ws, monkeypatch) -> None:
    from genesis_agent import edit_history
    _agent(ws / ".genesis" / "agents", "writer", "---\ndescription: пише\ntools: WRITE_FILE\n---\nx")
    seen_hooks: list[str] = []
    monkeypatch.setattr(hooks, "pre_tool", lambda tool, args: seen_hooks.append(tool) or None)
    edit_history.begin_turn("x")
    fake = _script(_Reply(calls=[_call("WRITE_FILE", {"path": "n.py", "content": "1\n"})]), _Reply("ок"))
    agents.run("writer", "x", ws, complete=fake)
    assert (ws / "n.py").exists() and seen_hooks == ["WRITE_FILE"]
    edit_history.undo()
    assert not (ws / "n.py").exists()


def test_unknown_agent_and_empty_task(ws) -> None:
    _agent(ws / ".genesis" / "agents", "reader", READER)
    assert "Има: reader" in agents.run("nope", "x", ws, complete=_script())
    assert "Няма задача" in agents.run("reader", "  ", ws, complete=_script())


def test_round_cap_ends_with_a_report_without_tools(ws) -> None:
    _agent(ws / ".genesis" / "agents", "reader", READER)
    tools_seen: list = []

    def fake(messages, tools=None):
        tools_seen.append(tools)
        return _Reply(calls=[_call("READ_FILE", {"path": "app.py"})]) if tools else _Reply("частично")
    out = agents.run("reader", "x", ws, complete=fake, max_rounds=3)
    assert tools_seen[-1] is None and "частично" in out


def test_light_model_is_used_when_asked(ws, monkeypatch) -> None:
    _agent(ws / ".genesis" / "agents", "quick", "---\ndescription: бърз\ntools: READ_FILE\nmodel: light\n---\nx")
    made: list[bool] = []

    class FakeBrain:
        def __init__(self, light: bool = False, **_kw) -> None:
            made.append(light)

        def complete(self, messages, tools=None):
            return _Reply("готово")
    monkeypatch.setattr("genesis_agent.brain.Brain", FakeBrain)
    assert "готово" in agents.run("quick", "x", ws)
    assert made == [True]


# ── главният агент ─────────────────────────────────────────────────────────

def test_the_main_agent_calls_it_natively_and_by_text_tag(ws, monkeypatch) -> None:
    _agent(ws / ".genesis" / "agents", "reader", READER)

    class FakeBrain:
        def __init__(self, **_kw) -> None:
            pass

        def complete(self, messages, tools=None):
            return _Reply(f"задача: {messages[1]['content']}")
    monkeypatch.setattr("genesis_agent.brain.Brain", FakeBrain)
    native = gs.dispatch_tool_call("AGENT", {"agent": "reader", "task": "намери цената"})
    text = gs.parse_and_execute_tools("[AGENT: reader | намери цената]")
    assert "задача: намери цената" in native and "задача: намери цената" in text[0]


def test_hooks_see_the_text_tag_like_the_native_call() -> None:
    assert gs._hook_view("AGENT", {"arg": "reader | намери"}) == \
        ("AGENT", {"agent": "reader", "task": "намери"})


def test_schema_and_prompt_only_when_agents_exist(ws) -> None:
    assert agents.schema(ws) is None and agents.prompt_section(ws) == ""
    assert "AGENT" not in {t["function"]["name"] for t in gta._turn_tools()}
    _agent(ws / ".genesis" / "agents", "reader", READER)
    schema = agents.schema(ws)
    assert schema is not None
    assert schema["function"]["parameters"]["properties"]["agent"]["enum"] == ["reader"]
    assert "reader — Чете кода" in agents.prompt_section(ws)
    assert "AGENT" in {t["function"]["name"] for t in gta._turn_tools()}


def test_agents_command_lists_them(ws) -> None:
    out: list[str] = []
    chat_commands.handle("/agents", messages=None, workspace=ws, out=out.append, ask=input)
    assert "Няма под-агенти" in out[0]
    _agent(ws / ".claude" / "agents", "rev", "---\ndescription: review\ntools: Read, Task\n---\nx")
    out.clear()
    chat_commands.handle("/agents", messages=None, workspace=ws, out=out.append, ask=input)
    assert "rev — review" in out[0] and "READ_FILE" in out[0] and "Task" in out[0]


# ── TODO_WRITE ─────────────────────────────────────────────────────────────

def test_todo_write_replaces_and_renders_the_list() -> None:
    out = todos.write([{"content": "прочети кода", "status": "completed"},
                       {"content": "оправи бъга", "status": "in_progress"},
                       {"content": "пусни тестовете", "status": "pending"}])
    assert "(1/3 готови)" in out
    assert "☑ прочети кода" in out and "▶ оправи бъга" in out and "☐ пусни тестовете" in out
    todos.write([{"content": "само това", "status": "pending"}])
    assert [i["content"] for i in todos.items()] == ["само това"]


def test_todo_write_refuses_an_unknown_status_and_keeps_the_old_list() -> None:
    todos.write([{"content": "a", "status": "pending"}])
    out = todos.write([{"content": "b", "status": "maybe"}])
    assert "Непознат статус" in out and todos.items()[0]["content"] == "a"


def test_todo_aliases_and_one_in_progress_note() -> None:
    out = todos.write([{"content": "a", "status": "done"}, {"content": "b", "status": "doing"},
                       {"content": "c", "status": "in-progress"}])
    assert [i["status"] for i in todos.items()] == ["completed", "in_progress", "in_progress"]
    assert "Една задача в работа" in out


def test_todo_text_tag_forms() -> None:
    assert todos.parse_text("[x] едно; [~] две; [ ] три") == [
        {"content": "едно", "status": "completed"}, {"content": "две", "status": "in_progress"},
        {"content": "три", "status": "pending"}]
    assert [i["status"] for i in todos.parse_text("- [x] a\n- b\n")] == ["completed", "pending"]
    assert todos.parse_text('[{"content": "a", "status": "completed"}]')[0]["status"] == "completed"


def test_todo_both_call_paths_and_plan_mode(ws) -> None:
    plan_mode.set_active(True)
    gs.dispatch_tool_call("TODO_WRITE", {"todos": [{"content": "план", "status": "in_progress"}]})
    assert todos.items() == [{"content": "план", "status": "in_progress"}]
    out = gs.parse_and_execute_tools("[TODO_WRITE: [x] план; [ ] изпълнение]")
    assert "(1/2 готови)" in out[0]


def test_todo_limits() -> None:
    todos.write([{"content": f"t{i}", "status": "pending"} for i in range(80)]
                + [{"content": "", "status": "pending"}])
    assert len(todos.items()) == todos._MAX_ITEMS
    todos.write([{"content": "x" * 1000, "status": "pending"}])
    assert len(todos.items()[0]["content"]) == todos._MAX_TEXT
    assert "❌" in todos.write({"content": "не е списък"})


def test_todos_command() -> None:
    out: list[str] = []
    chat_commands.handle("/todos", messages=None, workspace=Path("."), out=out.append, ask=input)
    assert "празен" in out[0]
