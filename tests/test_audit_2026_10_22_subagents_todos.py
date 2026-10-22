"""Одит 2026-10-22: под-агентите (.genesis/agents/) и списъкът със задачи.

Всеки тест падаше преди поправката си."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import agents, chat_commands, explore, hooks, paths, todos


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


# ── 1. счупена заглавка не дава всички инструменти ─────────────────────────

def test_a_broken_header_never_gives_the_agent_every_tool(ws) -> None:
    folder = ws / ".claude" / "agents"
    # Счупен YAML (двоеточие в description) + блоков списък tools.
    _agent(folder, "rev1", "---\nname: rev1\ndescription: Use this agent when: the user "
           "finished a change\ntools:\n  - Read\n  - Grep\n---\nReview only.\n")
    # Счупен YAML + flow списък.
    _agent(folder, "rev2", "---\nname: rev2\ndescription: Use this agent when: x\n"
           "tools: [Read, Grep]\n---\nReview only.\n")
    # Празен ред преди заглавката.
    _agent(folder, "rev3", "\n---\nname: rev3\ndescription: read only reviewer\n"
           "tools: Read, Grep\n---\nReview only.\n")
    # Незатворена заглавка.
    _agent(folder, "rev4", "---\nname: rev4\ndescription: read only reviewer\n"
           "tools: Read, Grep\n\nReview only.\n")
    # Счупен YAML с празно `tools:` и нищо след него — нищо, не всичко.
    _agent(folder, "rev5", "---\nname: rev5\ndescription: Use this agent when: x\n"
           "tools:\n---\nReview only.\n")
    # Счупен YAML + ключ в кавички / flow списък с кавички (преглед 2026-10-22).
    _agent(folder, "rev6", '---\nname: rev6\ndescription: Use this agent when: x\n'
           '"tools": Read, Grep\n---\nReview only.\n')
    _agent(folder, "rev7", '---\nname: rev7\ndescription: Use this agent when: x\n'
           'tools: ["Read", \'Grep\']\n---\nReview only.\n')
    found = agents.load(ws)
    read_only = frozenset({"READ_FILE", "SEARCH_CODE"})
    assert found["rev1"].tools == read_only
    assert found["rev2"].tools == read_only and found["rev2"].dropped == ()
    assert found["rev6"].tools == read_only
    assert found["rev7"].tools == read_only and found["rev7"].dropped == ()
    assert found["rev3"].tools == read_only and found["rev3"].prompt == "Review only."
    for name in ("rev4", "rev5"):
        a = found.get(name)
        assert a is None or not ({"WRITE_FILE", "RUN_CMD"} & agents.allowed_tools(a)), name
    fake = _script(_Reply(calls=[_call("WRITE_FILE", {"path": "pwned.py", "content": "x=1\n"})]),
                   _Reply("done"))
    agents.run("rev1", "review app.py", ws, complete=fake)
    assert not (ws / "pwned.py").exists()
    assert "няма този инструмент" in next(m for m in fake.seen[1] if m.get("role") == "tool")["content"]


# ── 2. един лош файл не сваля всички агенти ────────────────────────────────

def test_one_bad_agent_file_does_not_hide_the_others(ws) -> None:
    _agent(Path(paths.GENESIS_HOME) / "agents", "reviewer",
           "---\nname: reviewer\ndescription: operator's own reviewer\ntools: Read\n---\nReview.\n")
    repo = ws / ".claude" / "agents"
    # PyYAML хвърля ValueError за невъзможна дата и RecursionError за дълбоко влагане.
    _agent(repo, "helper", "---\nname: helper\ndescription: helps\nupdated: 2025-02-30\n---\nHelp.\n")
    _agent(repo, "deep", "---\nname: deep\ndescription: deep\ntools: " + "[" * 5000 + "]" * 5000
           + "\n---\nx\n")
    found = agents.load(ws)
    assert "reviewer" in found
    assert found["reviewer"].tools == frozenset({"READ_FILE"})
    deep = found.get("deep")
    assert deep is None or not ({"WRITE_FILE", "RUN_CMD"} & agents.allowed_tools(deep))
    assert "reviewer" in agents.prompt_section(ws)
    assert "AGENT" in {t["function"]["name"] for t in gta._turn_tools()}
    out: list[str] = []
    chat_commands.handle("/agents", messages=None, workspace=ws, out=out.append, ask=input)
    assert "reviewer" in out[0]


# ── 3. дългият доклад не се реже мълчаливо ─────────────────────────────────

def test_a_long_report_keeps_its_end_or_says_it_was_cut(ws) -> None:
    _agent(ws / ".genesis" / "agents", "all", "---\nname: all\ndescription: everything\n---\nDo it.\n")
    long = "\n".join(f"finding {i}: path/file{i}.py:{i} bug description padding padding"
                     for i in range(400))
    out = agents.run("all", "x", ws, complete=_script(_Reply(long)))
    assert "finding 399: path/file399.py:399" in out
    assert long in out or "отрязани" in out
    # EXPLORE — същото.
    q = explore.explore("къде?", str(ws), complete=_script(_Reply(long)))
    assert "finding 399: path/file399.py:399" in q and "отрязани" in q


# ── 4. празен TODO_WRITE не трие списъка ───────────────────────────────────

def test_a_blank_todo_write_does_not_wipe_the_list(ws) -> None:
    base = [{"content": "a", "status": "completed"}, {"content": "b", "status": "in_progress"}]
    todos.write(base)
    out = gs.parse_and_execute_tools("[TODO_WRITE: ]")
    assert "❌" in out[0] and todos.items() == base
    out2 = gs.dispatch_tool_call("TODO_WRITE", {"todos": "update"})
    assert "❌" in out2 and todos.items() == base
    # Изричните форми продължават да работят.
    gs.dispatch_tool_call("TODO_WRITE", {"todos": json.dumps(base[:1])})
    assert todos.items() == base[:1]
    gs.dispatch_tool_call("TODO_WRITE", {"todos": "[x] едно\n[ ] две"})
    assert [i["content"] for i in todos.items()] == ["едно", "две"]
    assert "празен" in gs.parse_and_execute_tools("[TODO_WRITE: []]")[0]
    assert todos.items() == []
