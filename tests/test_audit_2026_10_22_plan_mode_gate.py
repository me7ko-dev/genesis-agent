"""Одит 2026-10-22 (гейтът: hooks и правилата за разрешение).

Hooks, копирани от Claude Code, се проваляха „отворено“ без дума: JSON
решенията (deny/block) на изход 0 се лепяха като контекст, matcher-ите
Bash/Write/Edit не съвпадаха с нито един инструмент, `tool_input.file_path`
липсваше, а дробен или отрицателен `timeout` спираше блокиращия hook веднага.
Правило `uv run *` (и другите пускачи на пакети) се приемаше и пускаше всяка
команда без въпрос в `genesis -p`. Всеки тест тук падаше преди поправката."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import genesis_skills as gs
from genesis_agent import agents, hooks, permissions, sandbox
from genesis_agent.sandbox import SandboxPolicy

_BLOCK = "Спряно от hook"


def _home() -> Path:
    from genesis_agent import paths
    return Path(paths.GENESIS_HOME)


def _py(code: str) -> str:
    # Само ASCII и единични кавички в кода: същият ред върви и през cmd.exe.
    return f'"{sys.executable}" -c "{code}"'


def _write_hooks(table: dict) -> None:
    path = _home() / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": table}), encoding="utf-8")


def _prints(obj: dict) -> str:
    """Hook, който отпечатва JSON обекта и излиза с 0 (както в Claude Code)."""
    return _py(f"import json; print(json.dumps({obj!r}))")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    gs._SEEN_PATHS.clear()
    hooks.workspace = root
    permissions.workspace = root
    yield root
    hooks.workspace = None


# ── 1. JSON решенията на Claude Code (изход 0) ──────────────────────────────

def test_claude_code_json_decisions_block_like_exit_2(repo) -> None:
    deny = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "protected file"}}
    _write_hooks({"PreToolUse": [{"matcher": "WRITE_FILE",
                                  "hooks": [{"type": "command", "command": _prints(deny)}]}]})
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "a.txt", "content": "x\n"})
    assert _BLOCK in out and "protected file" in out
    assert not (repo / "a.txt").exists()

    _write_hooks({"PreToolUse": [{"matcher": "WRITE_FILE", "command": _prints(
        {"decision": "block", "reason": "legacy no"})}]})
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "b.txt", "content": "x\n"})
    assert _BLOCK in out and "legacy no" in out
    assert not (repo / "b.txt").exists()

    # „ask“: тук няма кого да пита — отказ, не тихо „да“.
    _write_hooks({"PreToolUse": [{"matcher": "WRITE_FILE", "command": _prints(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask"}})}]})
    assert _BLOCK in gs.dispatch_tool_call("WRITE_FILE", {"path": "q.txt", "content": "x\n"})
    assert not (repo / "q.txt").exists()

    # „allow“ не е отказ — и не заобикаля sandbox-а (той си решава сам).
    _write_hooks({"PreToolUse": [{"matcher": "WRITE_FILE", "command": _prints(
        {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}})}]})
    gs.dispatch_tool_call("WRITE_FILE", {"path": "c.txt", "content": "x\n"})
    assert (repo / "c.txt").exists()

    _write_hooks({
        "UserPromptSubmit": [{"command": _prints({"decision": "block", "reason": "secret"})}],
        "Stop": [{"command": _prints({"decision": "block", "reason": "tests are red"})}],
    })
    before = hooks.fire("UserPromptSubmit", {"prompt": "key sk-123"})
    assert before.blocked and before.message == "secret"
    assert before.context == ""     # JSON-ът не отива при модела като контекст
    stop = hooks.fire("Stop", {"last_assistant_message": "done"})
    assert stop.blocked and stop.message == "tests are red"

    # additionalContext е контекстът; обикновен текст си остава контекст.
    _write_hooks({"UserPromptSubmit": [
        {"command": _prints({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                    "additionalContext": "branch main"}})},
        {"command": _py("print('plain note')")},
    ]})
    ctx = hooks.fire("UserPromptSubmit", {"prompt": "hi"})
    assert not ctx.blocked
    assert ctx.context == "branch main\nplain note"


# ── 2. Пускачите на пакети не стават правило с `*` ──────────────────────────

@pytest.mark.parametrize("rule", [
    "uv run *", "uvx *", "npx *", "pnpx *", "bunx *", "bun x *", "npm exec *", "npm x *",
    "pnpm exec *", "pnpm dlx *", "yarn dlx *", "yarn exec *", "poetry run *", "pipx run *",
    "conda run *", "pdm run *", "hatch run *", "uv tool run *",
    "uv run --with pytest *", "npx -y *", "npm exec -- *",
    "uv run bash *", "poetry run python *", "npx sudo *",
])
def test_package_runner_rules_that_would_run_anything_are_refused(repo, rule) -> None:
    assert permissions.normalize_rule(rule)[0] is None
    assert permissions.add(rule, repo)[0] is None
    # Записано отпреди (с валиден подпис) — пак не се брои.
    permissions._save(permissions.project_key(repo), [" ".join(rule.split())])
    cmd = rule.removesuffix("*").replace("--with pytest ", "").strip() + " rm -rf build"
    policy = SandboxPolicy(mode="deny", confirm_fn=lambda op, v: True)
    assert not sandbox._decide(cmd, sandbox.assess_command(cmd), policy, shell=True)[0]


@pytest.mark.parametrize("rule", ["uv run pytest *", "npx prettier *", "poetry run pytest -q *",
                                  "uv run python -m pytest *", "npm exec eslint *"])
def test_a_runner_with_a_fixed_tool_is_still_a_rule(rule) -> None:
    assert permissions.normalize_rule(rule) == (rule, "")


# ── 3. Matcher-ите и полетата на Claude Code ────────────────────────────────

def test_claude_code_matchers_and_payload_reach_genesis_tools(repo) -> None:
    _write_hooks({"PreToolUse": [{"matcher": "Write|Edit|MultiEdit",
                                  "hooks": [{"type": "command", "command": "exit 2"}]}]})
    gs.dispatch_tool_call("WRITE_FILE", {"path": "w.txt", "content": "x\n"})
    assert not (repo / "w.txt").exists()

    _write_hooks({"PreToolUse": [{"matcher": "Bash", "command": "exit 2"}]})
    assert _BLOCK in gs.dispatch_tool_call("RUN_CMD", {"command": "echo hi"})
    # RUN_BG е RUN_CMD за hooks (_hook_view) — значи и Bash.
    bg = gs._before_tool("RUN_BG", {"arg": "echo hi"})
    assert isinstance(bg, str) and _BLOCK in bg

    _write_hooks({"PreToolUse": [{"matcher": "Read", "command": "exit 2"}]})
    gs.dispatch_tool_call("WRITE_FILE", {"path": "r.txt", "content": "x\n"})
    assert (repo / "r.txt").exists()      # Read не е Write

    # „Пази .env“ от Claude Code: чете tool_input.file_path и hook_event_name.
    _write_hooks({"PreToolUse": [{"matcher": "Write|Edit", "command": _py(
        "import json, sys; d = json.load(sys.stdin); "
        "p = d['tool_input'].get('file_path', ''); "
        "sys.exit(2 if p.endswith('.env') and d.get('hook_event_name') == 'PreToolUse' "
        "and d.get('cwd') else 0)")}]})
    gs.dispatch_tool_call("WRITE_FILE", {"path": ".env", "content": "SECRET=1\n"})
    assert not (repo / ".env").exists()
    gs.dispatch_tool_call("WRITE_FILE", {"path": "ok.txt", "content": "x\n"})
    assert (repo / "ok.txt").exists()

    # Същите имена, с които под-агентите четат `tools: Bash, Write` (agents.py).
    for genesis, names in hooks._CLAUDE_NAMES.items():
        for name in names:
            assert agents._CLAUDE_TOOLS[name.lower()] == genesis


# ── 4. Дробен или отрицателен timeout ───────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    (0.5, 1), (-1, 1), (2.5, 3), ("abc", 60), (float("inf"), 60), ("7", 7),
])
def test_a_fractional_or_negative_timeout_still_lets_the_hook_block(repo, raw, expected) -> None:
    _write_hooks({"PreToolUse": [{"matcher": "WRITE_FILE",
                                  "hooks": [{"type": "command", "command": "exit 2", "timeout": raw}]}]})
    (hook,) = hooks._parse(_home() / "hooks.json")
    assert hook.timeout == expected
    if expected == 1:
        out = gs.dispatch_tool_call("WRITE_FILE", {"path": "t.txt", "content": "x\n"})
        assert _BLOCK in out and not (repo / "t.txt").exists()
