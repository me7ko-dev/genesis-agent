"""What Claude Code gives its operator, in Genesis: project instructions
(GENESIS.md / AGENTS.md / CLAUDE.md), plan mode, /undo, hooks, own commands,
background commands."""
from __future__ import annotations

import contextlib
import json
import sys
import time
from collections import deque
from pathlib import Path

import pytest

import genesis_skills as gs
from genesis_agent import (
    background,
    chat_commands,
    edit_history,
    hooks,
    plan_mode,
    project_instructions,
)
from genesis_agent.paths import GENESIS_HOME  # noqa: F401  (patched per test)


def _home() -> Path:
    from genesis_agent import paths
    return Path(paths.GENESIS_HOME)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    ws = root / "app"
    ws.mkdir()
    monkeypatch.setattr(gs, "_WORKSPACE", ws)
    gs._SEEN_PATHS.clear()
    hooks.workspace = ws
    yield root, ws
    hooks.workspace = None


# ── project instructions ────────────────────────────────────────────────────

class TestProjectInstructions:
    def test_user_root_and_workspace_files_in_order(self, repo) -> None:
        root, ws = repo
        _home().joinpath("GENESIS.md").write_text("Отговаряй на български.", encoding="utf-8")
        (root / "AGENTS.md").write_text("Тестове: make test", encoding="utf-8")
        (ws / "GENESIS.md").write_text("Не пипай migrations/", encoding="utf-8")
        (ws / "GENESIS.local.md").write_text("Моят порт е 8001", encoding="utf-8")
        texts = [s.text for s in project_instructions.load(ws)]
        assert texts == ["Отговаряй на български.", "Тестове: make test",
                         "Не пипай migrations/", "Моят порт е 8001"]
        section = project_instructions.prompt_section(ws)
        assert section.index("make test") < section.index("migrations/")

    def test_one_file_per_folder_genesis_first(self, repo) -> None:
        root, _ = repo
        (root / "GENESIS.md").write_text("G", encoding="utf-8")
        (root / "CLAUDE.md").write_text("C", encoding="utf-8")
        assert [s.text for s in project_instructions.load(root)] == ["G"]

    def test_claude_md_is_read_when_it_is_the_only_one(self, repo) -> None:
        root, _ = repo
        (root / "CLAUDE.md").write_text("Use pnpm.", encoding="utf-8")
        assert "Use pnpm." in project_instructions.prompt_section(root)

    def test_nothing_above_the_repository_root(self, repo) -> None:
        root, ws = repo
        (root.parent / "GENESIS.md").write_text("чужд", encoding="utf-8")
        assert "чужд" not in project_instructions.prompt_section(ws)

    def test_imports_inside_the_project(self, repo) -> None:
        root, _ = repo
        (root / "docs").mkdir()
        (root / "docs" / "style.md").write_text("Табулации, не интервали.", encoding="utf-8")
        (root / "GENESIS.md").write_text("Стил: @docs/style.md\nmail: a@b.com", encoding="utf-8")
        text = project_instructions.prompt_section(root)
        assert "Табулации" in text and "### @docs/style.md" in text
        assert "### @b.com" not in text

    def test_imports_never_reach_secrets_or_outside(self, repo, tmp_path) -> None:
        root, _ = repo
        (root / ".env").write_text("API_KEY=sk-secret", encoding="utf-8")
        outside = tmp_path / "other.md"
        outside.write_text("извън проекта", encoding="utf-8")
        (root / "GENESIS.md").write_text(f"@.env @../other.md @{outside}", encoding="utf-8")
        text = project_instructions.prompt_section(root)
        assert "sk-secret" not in text and "извън проекта" not in text

    def test_size_is_capped(self, repo) -> None:
        root, _ = repo
        (root / "GENESIS.md").write_text("x" * 50_000, encoding="utf-8")
        assert len(project_instructions.prompt_section(root)) < project_instructions._MAX_TOTAL + 500


# ── plan mode ───────────────────────────────────────────────────────────────

class TestPlanMode:
    def test_changes_are_refused_reads_are_not(self, repo) -> None:
        _, ws = repo
        (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
        plan_mode.set_active(True)
        assert "Режим план" in gs.dispatch_tool_call("WRITE_FILE", {"path": "b.py", "content": "y"})
        assert "Режим план" in gs.dispatch_tool_call("RUN_CMD", {"command": "echo hi"})
        assert "Режим план" in gs.parse_and_execute_tools("[RUN_CMD: echo hi]")[0]
        assert not (ws / "b.py").exists()
        assert "x = 1" in gs.dispatch_tool_call("READ_FILE", {"path": "a.py"})

    def test_only_read_tools_are_offered(self) -> None:
        from genesis_agent.tool_schemas import FULL_TOOLS
        plan_mode.set_active(True)
        names = {t["function"]["name"] for t in plan_mode.filter_tools(FULL_TOOLS)}
        assert "READ_FILE" in names and not names & {"WRITE_FILE", "EDIT_FILE", "RUN_CMD"}
        plan_mode.set_active(False)
        assert plan_mode.filter_tools(FULL_TOOLS) is FULL_TOOLS

    def test_plan_command_toggles_and_runs_the_plan(self, repo) -> None:
        _, ws = repo
        seen: list[str] = []
        res = chat_commands.handle("/plan добави вход", messages=deque(), workspace=ws,
                                   out=seen.append, ask=lambda q: "")
        assert plan_mode.active() and res.prompt == "добави вход"
        res = chat_commands.handle("/plan", messages=deque(), workspace=ws,
                                   out=seen.append, ask=lambda q: "")
        assert not plan_mode.active() and "Изпълни" in res.prompt


# ── /undo ───────────────────────────────────────────────────────────────────

class TestUndo:
    def test_a_turn_of_writes_and_edits_goes_back(self, repo) -> None:
        _, ws = repo
        f = ws / "app.py"
        f.write_text("old\n", encoding="utf-8")
        edit_history.begin_turn("промени app")
        gs.dispatch_tool_call("READ_FILE", {"path": "app.py"})
        gs.dispatch_tool_call("EDIT_FILE", {"path": "app.py", "old": "old", "new": "new"})
        gs.dispatch_tool_call("WRITE_FILE", {"path": "new.py", "content": "print(1)\n"})
        edit_history.end_turn()
        assert f.read_text(encoding="utf-8") == "new\n" and (ws / "new.py").exists()
        out: list[str] = []
        res = chat_commands.handle("/undo", messages=deque(), workspace=ws,
                                   out=out.append, ask=lambda q: "")
        assert f.read_text(encoding="utf-8") == "old\n"
        assert not (ws / "new.py").exists()
        assert "отпреди" in res.messages[-1]["content"]

    def test_saying_no_changes_nothing(self, repo) -> None:
        _, ws = repo
        edit_history.begin_turn("x")
        gs.dispatch_tool_call("WRITE_FILE", {"path": "n.py", "content": "1"})
        edit_history.end_turn()
        chat_commands.handle("/undo", messages=deque(), workspace=ws,
                             out=lambda s: None, ask=lambda q: "не")
        assert (ws / "n.py").exists()

    def test_turns_go_back_one_at_a_time(self, repo) -> None:
        _, ws = repo
        f = ws / "v.txt"
        for v in ("1", "2"):
            edit_history.begin_turn(v)
            gs._SEEN_PATHS.add(f.resolve()) if f.exists() else None
            gs.dispatch_tool_call("WRITE_FILE", {"path": "v.txt", "content": v})
            edit_history.end_turn()
        edit_history.undo()
        assert f.read_text(encoding="utf-8") == "1"
        edit_history.undo()
        assert not f.exists()


# ── hooks ───────────────────────────────────────────────────────────────────

_BLOCK = "Спряно от hook"


def _py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def _write_hooks(path: Path, table: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": table}), encoding="utf-8")


class TestHooks:
    def test_pre_tool_exit_2_blocks_and_tells_the_model(self, repo) -> None:
        _, ws = repo
        _write_hooks(_home() / "hooks.json", {"PreToolUse": [{
            "matcher": "WRITE_FILE",
            "command": _py("import sys; sys.stderr.write('no writes in app/'); sys.exit(2)")}]})
        out = gs.dispatch_tool_call("WRITE_FILE", {"path": "x.py", "content": "1"})
        assert "no writes in app/" in out and not (ws / "x.py").exists()
        assert "Грешка" not in gs.dispatch_tool_call("LIST_DIR", {"path": "."})

    def test_post_tool_feedback_and_the_file_variable(self, repo) -> None:
        _, ws = repo
        _write_hooks(_home() / "hooks.json", {"PostToolUse": [{
            "matcher": "WRITE_FILE|EDIT_FILE",
            "command": _py("import os, sys; sys.stderr.write('lint: ' + os.environ['GENESIS_FILE']); "
                           "sys.exit(2)")}]})
        out = gs.dispatch_tool_call("WRITE_FILE", {"path": "y.py", "content": "1"})
        assert "lint: y.py" in out and (ws / "y.py").exists()

    def test_claude_code_nested_form_is_read(self, repo) -> None:
        _write_hooks(_home() / "hooks.json", {"PreToolUse": [{
            "matcher": "RUN_CMD", "hooks": [{"type": "command",
                                             "command": _py("import sys; sys.exit(2)")}]}]})
        assert _BLOCK in gs.dispatch_tool_call("RUN_CMD", {"command": "echo hi"})

    def test_project_hooks_need_trust_for_their_exact_content(self, repo) -> None:
        root, ws = repo
        project = root / ".genesis" / "hooks.json"
        _write_hooks(project, {"PreToolUse": [{"command": _py("import sys; sys.exit(2)")}]})
        assert _BLOCK not in gs.dispatch_tool_call("LIST_DIR", {"path": "."})
        res = chat_commands.handle("/hooks trust", messages=None, workspace=ws,
                                   out=lambda s: None, ask=lambda q: "да")
        assert res is not None
        assert _BLOCK in gs.dispatch_tool_call("LIST_DIR", {"path": "."})
        _write_hooks(project, {"PreToolUse": [{"command": _py("import sys; sys.exit(2)")}],
                               "Stop": []})
        assert _BLOCK not in gs.dispatch_tool_call("LIST_DIR", {"path": "."})

    def test_prompt_and_stop_hooks_around_a_turn(self, repo, monkeypatch) -> None:
        import genesis_terminal_agent as gta
        _write_hooks(_home() / "hooks.json", {
            "UserPromptSubmit": [{"command": _py("print('branch: main')")}],
            "Stop": [{"command": _py(
                "import json, sys; d = json.load(sys.stdin); "
                "sys.exit(0 if 'tests pass' in d['last_assistant_message'] else 2)")}],
        })
        replies = iter(["готово", "tests pass"])
        seen: list[str] = []

        def fake(messages, tools=None):
            seen.append(messages[-1]["content"])
            return next(replies), None
        monkeypatch.setattr(gta, "ask_genesis", fake)
        out = gta.run_turn(deque([{"role": "system", "content": "s"}]), "направи го", _ui(gta))
        assert "branch: main" in seen[0]
        assert "hook Stop" in seen[1]
        assert out[-1]["content"] == "tests pass"


def _ui(gta):
    class UI(gta.TurnUI):
        def thinking(self, label, spinner="dots"):
            return contextlib.nullcontext()
        def assistant(self, text): pass
        def tool(self, name, result): pass
        def asked(self, question): pass
        def spinning(self, note): pass
        def warn(self, text): pass
        def info(self, text): pass
        def cancelled(self): return False
    return UI()


# ── own commands ────────────────────────────────────────────────────────────

class TestOwnCommands:
    def test_project_command_with_arguments(self, repo) -> None:
        root, ws = repo
        cmds = root / ".genesis" / "commands"
        cmds.mkdir(parents=True)
        # Не „review“: /review е вграден и проектът не го подменя (2026-10-10).
        (cmds / "inspect.md").write_text(
            "---\ndescription: Преглед на файл\n---\nПрегледай $ARGUMENTS за грешки.", encoding="utf-8")
        res = chat_commands.handle("/inspect app.py", messages=deque(), workspace=ws,
                                   out=lambda s: None, ask=lambda q: "")
        assert res.prompt == "Прегледай app.py за грешки."
        listed: list[str] = []
        chat_commands.handle("/commands", messages=deque(), workspace=ws,
                             out=listed.append, ask=lambda q: "")
        assert any("Преглед на файл" in line for line in listed)

    def test_positional_arguments_and_no_placeholder(self, tmp_path) -> None:
        f = tmp_path / "c.md"
        f.write_text("Сравни $1 с $2", encoding="utf-8")
        assert chat_commands.expand_command(f, "a.py b.py") == "Сравни a.py с b.py"
        f.write_text("Обясни кода", encoding="utf-8")
        assert chat_commands.expand_command(f, "utils.py") == "Обясни кода\n\nutils.py"

    def test_unknown_slash_text_goes_to_the_model(self, repo) -> None:
        _, ws = repo
        assert chat_commands.handle("/nonexistent x", messages=deque(), workspace=ws,
                                    out=lambda s: None, ask=lambda q: "") is None

    def test_init_asks_the_model_to_write_genesis_md(self, repo) -> None:
        _, ws = repo
        res = chat_commands.handle("/init", messages=deque(), workspace=ws,
                                   out=lambda s: None, ask=lambda q: "")
        assert "GENESIS.md" in res.prompt


# ── background commands ─────────────────────────────────────────────────────

class TestBackground:
    def test_start_read_and_stop(self, repo) -> None:
        loop = _py("import time; [print('tick', i, flush=True) or time.sleep(0.2) for i in range(100)]")
        started = gs.dispatch_tool_call("RUN_CMD", {"command": loop, "background": True})
        assert "bg" in started, started
        job_id = started.split("като ", 1)[1].split(".", 1)[0]
        time.sleep(0.6)
        assert "tick" in gs.dispatch_tool_call("BG_OUTPUT", {"id": job_id})
        assert "върви" in background.summary()
        assert "спряна" in gs.dispatch_tool_call("BG_KILL", {"id": job_id})
        assert "върви" not in background.output(job_id).splitlines()[0]

    def test_the_log_folder_is_removed_at_exit(self, repo) -> None:
        """2026-10-09: папката с логовете оставаше в /tmp след всяка сесия."""
        started = gs.dispatch_tool_call("RUN_CMD", {"command": _py("print('x')"), "background": True})
        assert "bg" in started, started
        folder = background._dir
        assert folder is not None and folder.is_dir()
        deadline = time.monotonic() + 10   # на Windows жив процес държи лога отворен
        while background._running() and time.monotonic() < deadline:
            time.sleep(0.05)
        background.stop_all()
        assert not folder.exists() and background._dir is None

    def test_the_sandbox_gate_still_applies(self, repo) -> None:
        out = gs.parse_and_execute_tools("[RUN_BG: rm -rf /]")[0]
        assert "BLOCKED" in out or "отказ" in out.lower()


# ── MCP ─────────────────────────────────────────────────────────────────────

FAKE_MCP = Path(__file__).parent / "fake_mcp_server.py"
sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture
def mcp(repo, monkeypatch):
    from genesis_agent import mcp_client
    # cp1252 на stdin — както Python сървър на Windows (CI, 2026-10-08): кирилица
    # в аргументите трябва да стигне цяла.
    (_home() / "mcp.json").write_text(json.dumps({"mcpServers": {
        "fake": {"command": sys.executable, "args": [str(FAKE_MCP)],
                 "env": {"PYTHONIOENCODING": "cp1252"}}}}), encoding="utf-8")
    lines = mcp_client.start_all()
    assert any("2 инструмента" in line for line in lines), lines
    yield mcp_client
    mcp_client.stop_all()


class TestMCP:
    def test_tools_are_offered_with_their_schema(self, mcp) -> None:
        names = {s["function"]["name"] for s in mcp.schemas()}
        assert names == {"mcp__fake__echo", "mcp__fake__create_issue"}
        assert "fake.echo(text)" in mcp.prompt_section()

    def test_a_read_only_tool_runs_both_ways(self, mcp) -> None:
        assert gs.dispatch_tool_call("mcp__fake__echo", {"text": "здравей"}) == \
            "[MCP fake.echo] echo: здравей"
        assert gs.parse_and_execute_tools('[MCP: fake.echo | {"text": "hi"}]') == \
            ["[MCP fake.echo] echo: hi"]

    def test_a_changing_tool_asks_and_is_refused_unattended(self, mcp, monkeypatch) -> None:
        from genesis_agent import sandbox
        monkeypatch.setattr(sandbox._POLICY, "mode", "deny")
        out = gs.dispatch_tool_call("mcp__fake__create_issue", {"title": "bug"})
        assert "SANDBOX DENIED" in out and "done" not in out
        monkeypatch.setattr(sandbox._POLICY, "mode", "allow")
        assert "create_issue done bug" in gs.dispatch_tool_call("mcp__fake__create_issue", {"title": "bug"})

    def test_auto_approve_skips_the_question(self, repo, monkeypatch) -> None:
        from genesis_agent import mcp_client, sandbox
        (_home() / "mcp.json").write_text(json.dumps({"mcpServers": {"fake": {
            "command": sys.executable, "args": [str(FAKE_MCP)],
            "autoApprove": ["create_issue"]}}}), encoding="utf-8")
        mcp_client.start_all()
        monkeypatch.setattr(sandbox._POLICY, "mode", "deny")
        assert "create_issue done x" in gs.dispatch_tool_call("mcp__fake__create_issue", {"title": "x"})

    def test_plan_mode_keeps_only_read_only_mcp_tools(self, mcp) -> None:
        plan_mode.set_active(True)
        assert {s["function"]["name"] for s in mcp.schemas(read_only_only=True)} == {"mcp__fake__echo"}
        assert "echo: a" in gs.dispatch_tool_call("mcp__fake__echo", {"text": "a"})
        assert "Режим план" in gs.dispatch_tool_call("mcp__fake__create_issue", {"title": "x"})
        assert "Режим план" in gs.parse_and_execute_tools('[MCP: fake.create_issue | {"title": "x"}]')[0]

    def test_project_servers_need_trust(self, repo) -> None:
        from genesis_agent import mcp_client
        root, ws = repo
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": {
            "proj": {"command": sys.executable, "args": [str(FAKE_MCP)]}}}), encoding="utf-8")
        lines = mcp_client.start_all()
        assert any("не е доверен" in line for line in lines) and not mcp_client.tools()
        chat_commands.handle("/mcp trust", messages=None, workspace=ws,
                             out=lambda s: None, ask=lambda q: "да")
        assert any("proj" in line for line in mcp_client.start_all())
        assert mcp_client.tools()

    def test_a_server_that_does_not_start_is_reported(self, repo) -> None:
        from genesis_agent import mcp_client
        (_home() / "mcp.json").write_text(json.dumps({"mcpServers": {
            "broken": {"command": "definitely-not-a-command-xyz"}}}), encoding="utf-8")
        assert any("broken" in line and "не е намерен" in line for line in mcp_client.start_all())
        assert "Няма такъв" in gs.dispatch_tool_call("mcp__broken__x", {})


# ── EXPLORE ─────────────────────────────────────────────────────────────────

class _Reply:
    def __init__(self, text: str = "", calls: list | None = None) -> None:
        self.raw_text = text
        self.tool_calls = calls


def _call(name: str, args: dict, cid: str = "c1") -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class TestExplore:
    def test_it_searches_reads_and_returns_only_the_answer(self, repo) -> None:
        from genesis_agent import explore
        _, ws = repo
        (ws / "price.py").write_text("def total(x):\n    return x * 1.2\n", encoding="utf-8")
        seen_tools: list[list[str]] = []
        script = iter([
            _Reply(calls=[_call("SEARCH_CODE", {"pattern": "def total"})]),
            _Reply(calls=[_call("READ_FILE", {"path": "price.py"}, "c2"),
                          _call("WRITE_FILE", {"path": "x.py", "content": "1"}, "c3")]),
            _Reply("total() е в price.py:1 и добавя 20% ДДС."),
        ])

        def fake(messages, tools=None):
            seen_tools.append([t["function"]["name"] for t in tools or []])
            return next(script)
        out = explore.explore("къде се смята ДДС?", str(ws), complete=fake)
        assert out.endswith("total() е в price.py:1 и добавя 20% ДДС.")
        assert "2 прегледа" in out  # отказаният WRITE_FILE не е преглед
        assert not (ws / "x.py").exists()
        assert set(seen_tools[0]) == set(explore.TOOLS)

    def test_text_tag_models_work_too(self, repo) -> None:
        from genesis_agent import explore
        _, ws = repo
        (ws / "a.py").write_text("X = 1\n", encoding="utf-8")
        script = iter([_Reply("[SEARCH_CODE: X = 1]"), _Reply("X е в a.py:1")])
        out = explore.explore("къде е X?", str(ws), complete=lambda m, tools=None: next(script))
        assert out.endswith("X е в a.py:1")

    def test_rounds_are_capped(self, repo) -> None:
        from genesis_agent import explore
        _, ws = repo
        out = explore.explore("?", str(ws), max_rounds=3,
                              complete=lambda m, tools=None: _Reply(calls=[_call("LIST_DIR", {"path": "."})]))
        assert "спря след 3 рунда" in out

    def test_explore_is_allowed_in_plan_mode(self, repo, monkeypatch) -> None:
        import genesis_skills
        plan_mode.set_active(True)
        monkeypatch.setattr(genesis_skills, "_tool_explore", lambda q: f"answer to {q}")
        assert gs.dispatch_tool_call("EXPLORE", {"question": "q"}) == "answer to q"


# ── одит 2026-10-08: находките за PR #47 ────────────────────────────────────

def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("символните връзки не са позволени тук")


class TestAudit20261008:
    def test_a_symlinked_instruction_file_cannot_pull_in_outside_files(self, repo, tmp_path) -> None:
        root, ws = repo
        secret = tmp_path / "outside_secret.txt"
        secret.write_text("API_KEY=sk-live-123", encoding="utf-8")
        _symlink_or_skip(root / "GENESIS.md", secret)
        assert "sk-live-123" not in project_instructions.prompt_section(ws)

    def test_project_files_cannot_import_from_genesis_home(self, repo) -> None:
        root, _ = repo
        (_home() / "remote.json").write_text('{"key": "PAIRING-KEY"}', encoding="utf-8")
        (root / "AGENTS.md").write_text(f"@{_home() / 'remote.json'}", encoding="utf-8")
        assert "PAIRING-KEY" not in project_instructions.prompt_section(root)

    def test_home_is_never_the_project_root(self, tmp_path, monkeypatch) -> None:
        home = tmp_path / "home"
        (home / ".git").mkdir(parents=True)
        (home / ".config").mkdir()
        (home / ".config" / "rclone.conf").write_text("RCLONE-SECRET", encoding="utf-8")
        ws = home / "code" / "proj"
        ws.mkdir(parents=True)
        (ws / "AGENTS.md").write_text("@../../.config/rclone.conf", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        assert project_instructions._project_root(ws.resolve()) == ws.resolve()
        assert "RCLONE-SECRET" not in project_instructions.prompt_section(ws)

    def test_the_closest_file_survives_the_size_cap(self, repo) -> None:
        root, ws = repo
        _home().joinpath("GENESIS.md").write_text("g" * 9000, encoding="utf-8")
        (root / "GENESIS.md").write_text("r" * 9000, encoding="utf-8")
        (ws / "GENESIS.md").write_text("never touch migrations/", encoding="utf-8")
        assert "never touch migrations/" in project_instructions.prompt_section(ws)

    def test_trust_shows_everything_that_will_run(self, repo) -> None:
        root, ws = repo
        project = root / ".genesis" / "hooks.json"
        _write_hooks(project, {"Stop": [{"command": "pytest -q; true [a;touch PWNED;:]"}]})
        shown: list[str] = []
        chat_commands.handle("/hooks trust", messages=None, workspace=ws,
                             out=shown.append, ask=lambda q: "не")
        assert any("touch PWNED" in line for line in shown)

    def test_a_file_too_long_to_review_is_not_trusted(self, repo) -> None:
        root, ws = repo
        project = root / ".genesis" / "hooks.json"
        _write_hooks(project, {"Stop": [{"command": "echo " + "x" * 30000}]})
        chat_commands.handle("/hooks trust", messages=None, workspace=ws,
                             out=lambda s: None, ask=lambda q: "да")
        assert not hooks.is_trusted(project)

    def test_trust_does_not_travel_through_a_symlink(self, repo, tmp_path) -> None:
        root, ws = repo
        other = tmp_path / "A" / ".genesis"
        _write_hooks(other / "hooks.json", {"PreToolUse": [{"command": _py("import sys; sys.exit(2)")}]})
        hooks.trust(other / "hooks.json")
        (root / ".genesis").mkdir()
        _symlink_or_skip(root / ".genesis" / "hooks.json", other / "hooks.json")
        _, untrusted = hooks.configured(ws)
        assert untrusted is not None
        assert _BLOCK not in gs.dispatch_tool_call("LIST_DIR", {"path": "."})

    def test_a_block_holds_when_a_later_warning_cannot_be_shown(self, repo, monkeypatch) -> None:
        _, ws = repo
        _write_hooks(_home() / "hooks.json", {"PreToolUse": [
            {"command": _py("import sys; sys.exit(2)")},
            {"command": _py("import sys; sys.stderr.write('see [/etc/x]'); sys.exit(1)")}]})

        def broken(text):
            raise RuntimeError("markup")
        monkeypatch.setattr(hooks, "notify", broken)
        assert _BLOCK in gs.dispatch_tool_call("WRITE_FILE", {"path": "x.txt", "content": "1"})
        assert not (ws / "x.txt").exists()

    def test_run_bg_text_tag_meets_run_cmd_hooks(self, repo) -> None:
        _write_hooks(_home() / "hooks.json", {"PreToolUse": [
            {"matcher": "RUN_CMD", "command": _py("import sys; sys.exit(2)")}]})
        assert _BLOCK in gs.parse_and_execute_tools("[RUN_BG: echo hi]")[0]

    def test_background_false_as_text_runs_in_the_foreground(self, repo) -> None:
        out = gs.dispatch_tool_call("RUN_CMD", {"command": "echo done-sync", "background": "false"})
        assert "фона" not in out and "done-sync" in out

    def test_stop_hook_does_not_override_a_question_or_a_cancel(self, repo, monkeypatch) -> None:
        import genesis_terminal_agent as gta
        _write_hooks(_home() / "hooks.json", {"Stop": [{"command": _py("import sys; sys.exit(2)")}]})
        calls: list[int] = []

        def asks(messages, tools=None):
            calls.append(1)
            return "", [_call("ASK_USER", {"question": "кой файл?"})]
        monkeypatch.setattr(gta, "ask_genesis", asks)
        gta.run_turn(deque([{"role": "system", "content": "s"}]), "направи го", _ui(gta))
        assert len(calls) == 1

        calls.clear()
        monkeypatch.setattr(gta, "ask_genesis", lambda m, tools=None: (calls.append(1), ("ok", None))[1])
        ui = _ui(gta)
        ui.cancelled = lambda: True
        gta.run_turn(deque([{"role": "system", "content": "s"}]), "направи го", ui)
        assert len(calls) <= 1

    def test_a_refused_write_is_not_an_undo_step(self, repo) -> None:
        _, ws = repo
        edit_history.begin_turn("1")
        gs.dispatch_tool_call("WRITE_FILE", {"path": "a.txt", "content": "A"})
        edit_history.end_turn()
        (ws / "b.txt").write_text("old", encoding="utf-8")
        gs._SEEN_PATHS.clear()
        edit_history.begin_turn("2")
        assert "не си го чел" in gs.dispatch_tool_call("WRITE_FILE", {"path": "b.txt", "content": "B"})
        edit_history.end_turn()
        label, _done = edit_history.undo()
        assert label == "1" and not (ws / "a.txt").exists()

    def test_nothing_is_kept_outside_a_chat_turn(self, repo) -> None:
        gs.dispatch_tool_call("WRITE_FILE", {"path": "m.txt", "content": "1"})
        assert edit_history.pending() == []

    def test_compact_without_a_model_keeps_the_history(self, repo, monkeypatch) -> None:
        import genesis_terminal_agent as gta
        from genesis_agent import brain

        class _NoModel:
            def __init__(self, *a, **k): pass
            def complete(self, messages, tools=None):
                return _Reply("Error: no provider")
        monkeypatch.setattr(brain, "Brain", type("B", (brain.Brain,), {
            "__init__": _NoModel.__init__, "complete": _NoModel.complete}))
        msgs = deque([{"role": "system", "content": "s"}] +
                     [{"role": "user", "content": f"fact {i}"} for i in range(12)])
        shown: list[str] = []
        res = chat_commands.handle("/compact", messages=msgs, workspace=repo[1],
                                   out=shown.append, ask=lambda q: "", compact=gta._force_compact)
        assert res.messages is None and len(msgs) == 13
        assert any("не стана" in line for line in shown)

    def test_arguments_are_substituted_in_one_pass(self, tmp_path) -> None:
        f = tmp_path / "c.md"
        f.write_text("Fix this: $ARGUMENTS", encoding="utf-8")
        args = "price must be $5 and awk '{print $2}'"
        assert chat_commands.expand_command(f, args) == f"Fix this: {args}"

    def test_reserved_names_and_the_operators_own_command_win(self, repo) -> None:
        root, ws = repo
        for folder in (root / ".genesis" / "commands", _home() / "commands"):
            folder.mkdir(parents=True, exist_ok=True)
        (root / ".genesis" / "commands" / "help.md").write_text("x", encoding="utf-8")
        (root / ".genesis" / "commands" / "review.md").write_text("repo", encoding="utf-8")
        (_home() / "commands" / "review.md").write_text("mine", encoding="utf-8")
        found = chat_commands.custom_commands(ws)
        assert "help" not in found and found["review"].read_text(encoding="utf-8") == "mine"

    def test_bg_output_keeps_utf8_characters_whole(self, repo, tmp_path) -> None:
        import subprocess as sp
        log = tmp_path / "bg.log"
        log.write_bytes("здр".encode()[:3])
        proc = sp.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        background._jobs["bgx"] = background.Job("bgx", "x", proc, log)
        try:
            first = background.output("bgx")
            log.write_bytes("здравей".encode())
            second = background.output("bgx")
            assert "�" not in first + second
            assert "здравей" in (first.split("\n", 1)[1] + second.split("\n", 1)[1]).replace("(нищо ново)", "")
        finally:
            background._jobs.pop("bgx", None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_a_hook_that_times_out_takes_its_children_with_it(repo, tmp_path) -> None:
    import os
    pid_file = tmp_path / "child.pid"
    _write_hooks(_home() / "hooks.json", {"PreToolUse": [{
        "command": f"sleep 30 & echo $! > {pid_file}; wait", "timeout": 1}]})
    gs.dispatch_tool_call("LIST_DIR", {"path": "."})
    pid = int(pid_file.read_text())
    time.sleep(0.3)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    status = Path(f"/proc/{pid}/status")
    if alive and status.exists():  # убит, но неприбран от init (контейнер) — зомби
        alive = "\nState:\tZ" not in status.read_text()
    assert not alive


# ── одит 2026-10-08 (2): MCP и EXPLORE ──────────────────────────────────────

def _mcp_config(servers: dict) -> None:
    spec = {}
    for name, value in servers.items():
        spec[name] = value if isinstance(value, dict) else {
            "command": sys.executable, "args": [str(FAKE_MCP), *([value] if value else [])]}
    (_home() / "mcp.json").write_text(json.dumps({"mcpServers": spec}), encoding="utf-8")


class TestMCPAudit:
    def test_a_program_in_the_project_never_replaces_one_on_path(self, repo, monkeypatch) -> None:
        from genesis_agent import mcp_client
        root, ws = repo
        fake = root / ("npx.cmd" if sys.platform == "win32" else "npx")
        fake.write_text("echo PWNED", encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.chdir(root)
        env = {"PATH": f"{root}{__import__('os').pathsep}.", "PATHEXT": ".CMD;.EXE"}
        with pytest.raises(mcp_client.MCPError, match="не е намерен"):
            mcp_client._resolve_command("npx", env, [root, ws])

    def test_the_text_tag_meets_mcp_hooks(self, repo) -> None:
        from genesis_agent import mcp_client
        _mcp_config({"fake": ""})
        mcp_client.start_all()
        _write_hooks(_home() / "hooks.json", {"PreToolUse": [
            {"matcher": "mcp__fake__.*", "command": _py("import sys; sys.exit(2)")}]})
        assert _BLOCK in gs.dispatch_tool_call("mcp__fake__echo", {"text": "a"})
        assert _BLOCK in gs.parse_and_execute_tools('[MCP: fake.echo | {"text": "a"}]')[0]

    def test_a_chatty_server_cannot_hold_the_start(self, repo, monkeypatch) -> None:
        from genesis_agent import mcp_client
        monkeypatch.setattr(mcp_client, "_START_TIMEOUT", 1.5)
        _mcp_config({"chatty": "chatty", "ok": ""})
        started = time.monotonic()
        lines = mcp_client.start_all()
        assert time.monotonic() - started < 8
        assert any(line.startswith("⚠ MCP chatty") for line in lines)
        assert any("ok: 2 инструмента" in line for line in lines)

    def test_one_bad_server_or_field_does_not_switch_off_the_rest(self, repo) -> None:
        from genesis_agent import mcp_client
        _mcp_config({"strerr": "strerr", "ok": "",
                     "weird": {"command": sys.executable, "autoApprove": True}})
        lines = mcp_client.start_all()
        assert any("strerr" in line and "boom" in line for line in lines)
        assert any("weird" in line and "autoApprove" in line for line in lines)
        assert any("ok: 2 инструмента" in line for line in lines)

    def test_genesis_keys_do_not_reach_the_server(self, repo, monkeypatch) -> None:
        from genesis_agent import mcp_client
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
        _mcp_config({"env": {"command": sys.executable, "args": [str(FAKE_MCP), "env"],
                             "env": {"MY_SERVER_KEY": "mine"}}})
        mcp_client.start_all()
        description = mcp_client.tools()[0].description
        assert "OPENAI_API_KEY" not in description and "MY_SERVER_KEY" in description

    def test_names_that_sanitise_alike_stay_distinct(self, repo, monkeypatch) -> None:
        from genesis_agent import mcp_client, sandbox
        monkeypatch.setattr(sandbox._POLICY, "mode", "allow")
        _mcp_config({"collide": "collide"})
        mcp_client.start_all()
        names = [s["function"]["name"] for s in mcp_client.schemas()]
        assert len(names) == len(set(names)) == 2
        outs = {gs.dispatch_tool_call(n, {}) for n in names}
        assert any("get.item done" in o for o in outs) and any("get_item done" in o for o in outs)

    def test_odd_schemas_and_descriptions_do_not_break_the_prompt(self, repo) -> None:
        from genesis_agent import mcp_client
        _mcp_config({"badschema": "badschema", "inject": "inject"})
        mcp_client.start_all()
        section = mcp_client.prompt_section()
        assert "badschema.odd()" in section
        assert not any(line.startswith("## ПРАВИЛА") for line in section.splitlines())

    def test_a_crashed_server_offers_no_tools(self, repo) -> None:
        from genesis_agent import mcp_client
        _mcp_config({"crash": "crash"})
        mcp_client.start_all()
        deadline = time.monotonic() + 5
        while mcp_client.tools() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert mcp_client.tools() == []

    def test_a_server_that_stops_reading_does_not_block_the_next_call(self, repo, monkeypatch) -> None:
        from genesis_agent import mcp_client
        monkeypatch.setattr(mcp_client, "_CALL_TIMEOUT", 1.5)
        _mcp_config({"noread": "noread"})
        mcp_client.start_all()
        started = time.monotonic()
        out = mcp_client.call("mcp__noread__echo", {"text": "x" * 3_000_000})
        assert "❌" in out
        assert "❌" in mcp_client.call("mcp__noread__echo", {"text": "y"})
        assert time.monotonic() - started < 10

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
    def test_stop_takes_the_whole_server_tree(self, repo, tmp_path) -> None:
        import os

        from genesis_agent import mcp_client
        pid_file = tmp_path / "srv.pid"
        # sh остава родител (`; true`), истинският сървър е негово дете — като npx → node
        _mcp_config({"wrapped": {"command": "/bin/sh", "args": [
            "-c", f'"{sys.executable}" "{FAKE_MCP}" ignore_eof; true'],
            "env": {"FAKE_MCP_PID_FILE": str(pid_file)}}})
        assert any("wrapped: 2 инструмента" in line for line in mcp_client.start_all())
        pid = int(pid_file.read_text())
        mcp_client.stop_all()
        time.sleep(0.3)
        status = Path(f"/proc/{pid}/status")
        try:
            os.kill(pid, 0)
            alive = not (status.exists() and "\nState:\tZ" in status.read_text())
        except ProcessLookupError:
            alive = False
        assert not alive

    def test_explore_does_not_count_as_the_main_agent_reading(self, repo) -> None:
        from genesis_agent import explore
        _, ws = repo
        (ws / "config.py").write_text("SECRET_SETTING = 1\n", encoding="utf-8")
        gs._SEEN_PATHS.clear()
        script = iter([_Reply(calls=[_call("READ_FILE", {"path": "config.py"})]), _Reply("done")])
        explore.explore("?", str(ws), complete=lambda m, tools=None: next(script))
        assert "не си го чел" in gs.dispatch_tool_call("WRITE_FILE", {"path": "config.py", "content": "x"})

    def test_explore_reads_paths_with_brackets(self, repo) -> None:
        from genesis_agent import explore
        _, ws = repo
        page = ws / "app" / "[id]"
        page.mkdir(parents=True)
        (page / "page.tsx").write_text("export default 42\n", encoding="utf-8")
        seen: list[str] = []

        def fake(messages, tools=None):
            if len(messages) == 2:
                return _Reply("[READ_FILE: app/[id]/page.tsx]")
            seen.append(messages[-1]["content"])
            return _Reply("ok")
        explore.explore("?", str(ws), complete=fake)
        assert "export default 42" in seen[0]


# ── MCP по HTTP ─────────────────────────────────────────────────────────────

@pytest.fixture
def http_mcp(repo, monkeypatch):
    import fake_mcp_http
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    servers = []

    def start(mode="json", token="", headers=None):
        url, httpd, state = fake_mcp_http.serve(mode, token)
        servers.append(httpd)
        _mcp_config({"remote": {"type": "http", "url": url, **({"headers": headers} if headers else {})}})
        return state
    yield start
    for h in servers:
        h.shutdown()


class TestMCPOverHTTP:
    @pytest.mark.parametrize("mode", ["json", "sse"])
    def test_tools_list_and_call(self, http_mcp, mode) -> None:
        from genesis_agent import mcp_client
        state = http_mcp(mode)
        assert any("remote: 1 инструмента" in line for line in mcp_client.start_all())
        assert gs.dispatch_tool_call("mcp__remote__echo", {"text": "здравей"}) == "[MCP remote.echo] echo: здравей"
        if mode == "sse":
            assert state["pings_answered"] >= 1
        mcp_client.stop_all()
        assert state["deleted"]

    def test_a_token_from_the_environment(self, http_mcp, monkeypatch) -> None:
        from genesis_agent import mcp_client
        monkeypatch.setenv("FAKE_MCP_TOKEN", "s3cret")
        http_mcp("json", token="s3cret", headers={"Authorization": "Bearer ${FAKE_MCP_TOKEN}"})
        assert any("remote: 1 инструмента" in line for line in mcp_client.start_all())

    def test_a_wrong_token_is_reported(self, http_mcp) -> None:
        from genesis_agent import mcp_client
        http_mcp("json", token="s3cret", headers={"Authorization": "Bearer wrong"})
        assert any("401" in line for line in mcp_client.start_all())

    def test_old_sse_transport_is_explained(self, repo) -> None:
        from genesis_agent import mcp_client
        _mcp_config({"old": {"type": "sse", "url": "https://x.example/sse"}})
        assert any("старият транспорт" in line for line in mcp_client.start_all())
