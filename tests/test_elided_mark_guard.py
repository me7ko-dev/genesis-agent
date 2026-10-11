"""B.12 (2026-10-11): маркерът за изпуснато от историята никога не става файл.

budget._elide_old_writes заменя съдържанието на стари WRITE_FILE/EDIT_FILE в
изпратеното копие с маркер. Слаб модел в текстов режим вижда
WRITE_FILE({path, content: <маркер>…}) и може да го препише — пазачът в
genesis_skills._before_tool отказва такъв запис и по двата пътя, винаги.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import genesis_skills as gs
from genesis_agent import budget, hooks

MARK = budget.ELIDED_MARK + " 3862 символа — НЕ е съдържание; текущият вид: READ_FILE a.py⟧"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    gs._SEEN_PATHS.clear()
    hooks.workspace = root
    yield root
    hooks.workspace = None


def test_a_native_write_of_the_mark_is_refused(ws) -> None:
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "a.py", "content": MARK})
    assert "⛔" in out and "READ_FILE" in out
    assert not (ws / "a.py").exists()


def test_a_native_edit_whose_new_text_is_the_mark_is_refused(ws) -> None:
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    out = gs.dispatch_tool_call("EDIT_FILE", {"path": "a.py", "old": "x = 1", "new": MARK})
    assert "⛔" in out
    assert (ws / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_the_text_tag_write_is_refused_too(ws) -> None:
    out = gs.parse_and_execute_tools(f"[WRITE_FILE: a.py]{MARK}[END_WRITE]")
    assert any("⛔" in r for r in out)
    assert not (ws / "a.py").exists()


def test_the_text_tag_edit_is_refused_too(ws) -> None:
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    out = gs.parse_and_execute_tools(
        f"[EDIT_FILE: a.py]x = 1{gs._EDIT_SEPARATOR}{MARK}[END_EDIT]")
    assert any("⛔" in r for r in out)
    assert (ws / "a.py").read_text(encoding="utf-8") == "x = 1\n"


def test_a_heredoc_command_carrying_the_mark_is_refused(ws) -> None:
    # Само ASCII в командата: на Windows cmd.exe чупи кирилицата (CLAUDE.md).
    mark = budget.ELIDED_MARK.encode("ascii", "ignore").decode() + " 3862 chars"
    out = gs.dispatch_tool_call("RUN_CMD", {"command": f"cat > a.py <<'EOF'\n{mark}\nEOF"})
    assert "⛔" in out
    assert not (ws / "a.py").exists()


def test_an_ordinary_write_still_goes_through(ws) -> None:
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "a.py", "content": "print(1)\n"})
    assert "⛔" not in out
    assert (ws / "a.py").read_text(encoding="utf-8") == "print(1)\n"


def test_no_tracked_python_file_carries_the_joined_tag() -> None:
    """Иначе Genesis не би могъл да редактира собствения си код: всеки запис на
    такъв файл би бил отказан от пазача."""
    root = Path(__file__).resolve().parent.parent
    try:
        listed = subprocess.run(["git", "ls-files", "*.py"], cwd=root, capture_output=True,
                                text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("няма git")
    files = [root / f for f in listed]
    assert files
    offenders = [str(f) for f in files
                 if f.is_file() and budget.ELIDED_TAG in f.read_text(encoding="utf-8", errors="replace")]
    assert offenders == []
