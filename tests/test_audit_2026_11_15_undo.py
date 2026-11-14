"""/undo, одит 2026-11-15 (NEXT_STEPS: две дупки, възпроизведени на 2026-11-10):
1. EDIT + `chmod` в същия ход — съдържанието се връщаше, правата оставаха;
2. команда сменя файл с връзка (`rm f && ln -s /elsewhere f`) — pending()
   обещаваше „върни“, undo оставяше връзката. Обещание и действие трябва да
   съвпадат, а през връзката към външния файл пак не се пише."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

from genesis_agent import edit_history

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="няма git")


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "f.sh").write_text("echo v1\n", encoding="utf-8")
    (root / "f.txt").write_text("v1\n", encoding="utf-8")
    return root


def _symlink_or_skip(target: Path, link: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("няма права за символни връзки (Windows без Developer Mode)")


def _edit(path: Path, text: str) -> None:
    edit_history.record(path)
    path.write_text(text, encoding="utf-8")
    edit_history.forget_if_unchanged(path)


def _in_command(ws: Path, change) -> None:
    edit_history.before_command(ws)
    change()
    edit_history.after_command()


@pytest.mark.skipif(os.name == "nt", reason="изпълним бит има само на POSIX")
def test_edit_plus_chmod_x_restores_the_mode(ws) -> None:
    f = ws / "f.sh"
    f.chmod(0o644)
    edit_history.begin_turn("edit и chmod +x")
    _edit(f, "echo v2\n")
    _in_command(ws, lambda: f.chmod(0o755))
    edit_history.end_turn()
    edit_history.undo()
    assert f.read_text(encoding="utf-8") == "echo v1\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="изпълним бит има само на POSIX")
def test_edit_plus_chmod_minus_x_restores_the_exec_bit(ws) -> None:
    f = ws / "f.sh"
    f.chmod(0o755)
    edit_history.begin_turn("edit и chmod -x")
    _edit(f, "echo v2\n")
    _in_command(ws, lambda: f.chmod(0o644))
    edit_history.end_turn()
    edit_history.undo()
    assert stat.S_IMODE(f.stat().st_mode) == 0o755


def test_edit_plus_readonly_restores_writable_on_every_platform(ws) -> None:
    """Само read-only битът е смислен и на Windows."""
    f = ws / "f.txt"
    edit_history.begin_turn("edit и read-only")
    _edit(f, "v2\n")
    _in_command(ws, lambda: f.chmod(stat.S_IREAD))
    edit_history.end_turn()
    try:
        _label, lines = edit_history.undo()
        assert f.read_text(encoding="utf-8") == "v1\n", lines
        assert os.access(f, os.W_OK)
    finally:
        f.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_file_created_in_the_turn_is_still_removed(ws) -> None:
    n = ws / "new.sh"
    edit_history.begin_turn("нов файл")
    _edit(n, "x\n")
    n.chmod(0o755)
    edit_history.end_turn()
    edit_history.undo()
    assert not n.exists()


def test_link_replacing_an_edited_file_is_removed_and_the_file_comes_back(ws, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    shared = outside / "shared.txt"
    shared.write_text("на оператора\n", encoding="utf-8")
    f = ws / "f.txt"

    def swap() -> None:
        f.unlink()
        _symlink_or_skip(shared, f)

    edit_history.begin_turn("edit, после връзка на мястото")
    _edit(f, "v2\n")
    _in_command(ws, swap)
    edit_history.end_turn()
    plan = edit_history.pending()
    assert any("f.txt" in ln and "връзка" in ln for ln in plan), plan   # честно за връзката
    _label, lines = edit_history.undo()
    assert not f.is_symlink(), lines
    assert f.read_text(encoding="utf-8") == "v1\n"
    assert shared.read_text(encoding="utf-8") == "на оператора\n"       # нищо през връзката
    assert any("връзка" in ln for ln in lines)


def test_link_to_a_file_inside_the_project_is_also_replaced(ws) -> None:
    f = ws / "f.txt"

    def swap() -> None:
        f.unlink()
        _symlink_or_skip(ws / "f.sh", f)

    edit_history.begin_turn("edit, после връзка към файл в проекта")
    _edit(f, "v2\n")
    _in_command(ws, swap)
    edit_history.end_turn()
    edit_history.undo()
    assert not f.is_symlink() and f.read_text(encoding="utf-8") == "v1\n"
    assert (ws / "f.sh").read_text(encoding="utf-8") == "echo v1\n"


def test_link_the_operator_repointed_after_the_turn_is_left(ws, tmp_path) -> None:
    other = tmp_path / "other.txt"
    other.write_text("друг\n", encoding="utf-8")
    shared = tmp_path / "shared.txt"
    shared.write_text("общ\n", encoding="utf-8")
    f = ws / "f.txt"

    def swap() -> None:
        f.unlink()
        _symlink_or_skip(shared, f)

    edit_history.begin_turn("edit, после връзка")
    _edit(f, "v2\n")
    _in_command(ws, swap)
    edit_history.end_turn()
    f.unlink()
    os.symlink(other, f)                      # операторът я пренасочи
    edit_history.undo()
    assert f.is_symlink() and os.readlink(f) == str(other)
