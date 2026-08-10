"""genesis_skills.py — the bridge between the terminal agent's tool tags and
the sandboxed implementations. Untested until now despite being the module
every LLM-generated action actually flows through: a bug here either corrupts
a file silently or raises where the docstrings promise it never will.

Three properties matter most, and are what these tests are built around:
  1. WRITE_FILE/EDIT_FILE outside the workspace go through the same
     SAFE/CONFIRM/BLOCKED gate as shell commands — not a free pass just
     because the tool "only" touches a file.
  2. dispatch_tool_call() never raises — an unknown tool or a broken argument
     comes back as a `[TOOL] message` string the model can react to, per its
     own docstring.
  3. parse_and_execute_readonly_tools() really is read-only: a RUN_CMD tag
     embedded in autonomous-mode text must not execute.
"""
from __future__ import annotations

import pytest

import genesis_skills as gs


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    """Every test gets its own throwaway workspace instead of the real
    project root `set_workspace` defaults to at import time."""
    gs.set_workspace(tmp_path)
    monkeypatch.setattr("genesis_agent.sandbox._POLICY", gs.sandbox.SandboxPolicy())
    yield tmp_path
    gs.set_workspace(gs._PROJECT_ROOT)


# ── _resolve ──────────────────────────────────────────────────────────────

def test_resolve_relative_path_is_relative_to_workspace(_workspace) -> None:
    assert gs._resolve("sub/file.txt") == _workspace / "sub/file.txt"


def test_resolve_absolute_path_passes_through(_workspace, tmp_path_factory) -> None:
    other = tmp_path_factory.mktemp("elsewhere") / "f.txt"
    assert gs._resolve(str(other)) == other


# ── _tool_read_file ──────────────────────────────────────────────────────

def test_read_file_returns_contents(_workspace) -> None:
    (_workspace / "note.txt").write_text("hello genesis", encoding="utf-8")
    out = gs._tool_read_file("note.txt")
    assert "hello genesis" in out


def test_read_file_missing_is_a_message_not_an_exception(_workspace) -> None:
    out = gs._tool_read_file("nope.txt")
    assert "не съществува" in out


def test_read_file_truncates_past_8000_chars(_workspace) -> None:
    (_workspace / "big.txt").write_text("x" * 9000, encoding="utf-8")
    out = gs._tool_read_file("big.txt")
    assert "отрязано" in out
    assert "x" * 9000 not in out
    assert "x" * 8000 in out


# ── _tool_write_file ─────────────────────────────────────────────────────

def test_write_file_creates_file_inside_workspace(_workspace) -> None:
    out = gs._tool_write_file("notes/todo.txt", "buy milk")
    assert "✓" in out
    assert (_workspace / "notes/todo.txt").read_text(encoding="utf-8") == "buy milk"


def test_write_file_outside_workspace_is_denied_non_interactively(_workspace, tmp_path_factory) -> None:
    """No tty in a pytest run → sandbox mode resolves to 'deny' for CONFIRM.
    The write must not happen at all, not just warn after the fact."""
    outside = tmp_path_factory.mktemp("outside") / "evil.txt"
    out = gs._tool_write_file(str(outside), "should not land")
    assert not outside.exists()
    assert "SANDBOX DENIED" in out or "[WRITE_FILE]" in out


# ── _tool_edit_file ──────────────────────────────────────────────────────

def test_edit_file_replaces_the_anchor(_workspace) -> None:
    f = _workspace / "app.py"
    f.write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    out = gs._tool_edit_file("app.py", "return 'hi'", "return 'hello'")
    assert "❌" not in out
    assert "return 'hello'" in f.read_text(encoding="utf-8")


def test_edit_file_missing_anchor_leaves_file_untouched(_workspace) -> None:
    f = _workspace / "app.py"
    original = "def greet():\n    return 'hi'\n"
    f.write_text(original, encoding="utf-8")
    out = gs._tool_edit_file("app.py", "this text is not in the file", "replacement")
    assert "❌" in out
    assert f.read_text(encoding="utf-8") == original


# ── dispatch_tool_call ───────────────────────────────────────────────────

def test_dispatch_accepts_dict_arguments(_workspace) -> None:
    (_workspace / "a.txt").write_text("dict-args", encoding="utf-8")
    out = gs.dispatch_tool_call("READ_FILE", {"path": "a.txt"})
    assert "dict-args" in out


def test_dispatch_accepts_raw_json_string_arguments(_workspace) -> None:
    (_workspace / "a.txt").write_text("json-args", encoding="utf-8")
    out = gs.dispatch_tool_call("READ_FILE", '{"path": "a.txt"}')
    assert "json-args" in out


def test_dispatch_invalid_json_string_does_not_raise(_workspace) -> None:
    out = gs.dispatch_tool_call("READ_FILE", "{not json")
    assert "Невалидни аргументи" in out


def test_dispatch_unknown_tool_name_does_not_raise(_workspace) -> None:
    out = gs.dispatch_tool_call("NOT_A_REAL_TOOL", {})
    assert "Непознат tool" in out


def test_dispatch_never_raises_even_when_the_impl_blows_up(_workspace, monkeypatch) -> None:
    """Docstring promise: 'Никога не хвърля — грешка връща като низ.'"""
    def _boom(_path):
        raise RuntimeError("disk on fire")
    monkeypatch.setattr(gs, "_tool_read_file", _boom)
    out = gs.dispatch_tool_call("READ_FILE", {"path": "a.txt"})
    assert "Грешка при изпълнение" in out


# ── tag parsing: parse_and_execute_tools ────────────────────────────────

def test_write_file_tag_round_trips_through_tag_parsing(_workspace) -> None:
    text = "[WRITE_FILE: out.txt]hello from a tag[END_WRITE]"
    results = gs.parse_and_execute_tools(text)
    assert len(results) == 1
    assert (_workspace / "out.txt").read_text(encoding="utf-8") == "hello from a tag"


def test_tool_tags_inside_a_write_file_body_are_not_separately_executed(_workspace) -> None:
    """A WRITE_FILE body that happens to *contain* another tag (e.g. the model
    is writing documentation about the tool syntax) must not also fire that
    tag as a second tool call — only the outer WRITE_FILE should run."""
    text = "[WRITE_FILE: doc.txt]Example: [READ_FILE: secrets.txt][END_WRITE]"
    results = gs.parse_and_execute_tools(text)
    assert len(results) == 1
    assert "[READ_FILE: secrets.txt]" in (_workspace / "doc.txt").read_text(encoding="utf-8")


def test_multiple_tags_execute_in_the_order_they_appear(_workspace) -> None:
    (_workspace / "first.txt").write_text("1st", encoding="utf-8")
    (_workspace / "second.txt").write_text("2nd", encoding="utf-8")
    text = "[READ_FILE: first.txt]\nsome text in between\n[READ_FILE: second.txt]"
    results = gs.parse_and_execute_tools(text)
    assert len(results) == 2
    assert "1st" in results[0]
    assert "2nd" in results[1]


# ── tag parsing: parse_and_execute_readonly_tools (autonomous-loop gate) ──

def test_readonly_parser_runs_read_file(_workspace) -> None:
    (_workspace / "r.txt").write_text("readable", encoding="utf-8")
    results = gs.parse_and_execute_readonly_tools("[READ_FILE: r.txt]")
    assert len(results) == 1
    assert "readable" in results[0]


def test_readonly_parser_ignores_run_cmd(_workspace, tmp_path) -> None:
    """The whole point of the readonly parser: an autonomous mission must not
    be able to smuggle a shell command through it."""
    marker = tmp_path / "should_not_exist.txt"
    text = f"[RUN_CMD: touch {marker}]"
    results = gs.parse_and_execute_readonly_tools(text)
    assert results == []
    assert not marker.exists()


def test_readonly_parser_ignores_write_file(_workspace) -> None:
    text = "[WRITE_FILE: sneaky.txt]should not be written[END_WRITE]"
    results = gs.parse_and_execute_readonly_tools(text)
    assert results == []
    assert not (_workspace / "sneaky.txt").exists()
