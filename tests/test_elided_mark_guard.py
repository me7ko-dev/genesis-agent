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


# Голото ASCII име на маркера не е маркерът (ревю 2026-10-11): пазачът
# отказваше търсене за него, commit съобщение и бележка в .md, която го
# назовава, с подвеждащото „прочети файла с READ_FILE“. Хваща се само формата,
# която budget._elide_old_writes наистина слага: „⟦“ + името, или името + брой.
@pytest.mark.parametrize("command", [
    f"git grep -n {budget.ELIDED_TAG} -- genesis_skills.py",
    f'git commit -m "guard: refuse the {budget.ELIDED_TAG} marker"',
    f'git commit -m "{budget.ELIDED_TAG} 2026-10-11: narrower guard"',
])
def test_a_command_that_only_names_the_tag_is_not_refused(command) -> None:
    assert gs._copies_elided_mark("RUN_CMD", {"command": command}) is None


def test_a_doc_that_names_the_tag_can_be_written(ws) -> None:
    text = f"# Бележки\n\nB.12: маркерът `{budget.ELIDED_TAG}` се изпуска само в копието.\n"
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "NOTES.md", "content": text})
    assert "⛔" not in out
    assert (ws / "NOTES.md").read_text(encoding="utf-8") == text


@pytest.mark.parametrize("text", [
    MARK,
    budget.ELIDED_MARK,
    budget.ELIDED_TAG + " 3862 chars",
    f"x = 1\n{budget.ELIDED_TAG}  12\tsymbols\n",
    f"x = 1\n{budget.ELIDED_TAG} 3862",
])
def test_every_shape_of_the_generated_mark_is_caught(text) -> None:
    assert budget.carries_elided_mark(text)
    assert gs._copies_elided_mark("WRITE_FILE", {"path": "a.py", "content": text})


def test_no_tracked_file_carries_the_mark() -> None:
    """Иначе Genesis не би могъл да редактира собствения си код и бележки: всеки
    запис на такъв файл би бил отказан от пазача. Сканира се с проверката на
    самия пазач, по всички следени файлове, не само *.py (ревю 2026-10-11)."""
    root = Path(__file__).resolve().parent.parent
    try:
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                                text=True, check=True).stdout.split("\0")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("няма git")
    files = [root / f for f in listed if f]
    assert files
    offenders = [str(f) for f in files
                 if f.is_file() and budget.carries_elided_mark(
                     f.read_text(encoding="utf-8", errors="replace"))]
    assert offenders == []
