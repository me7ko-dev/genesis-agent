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
    # Не os.readlink(f) == str(other): на Windows readlink връща `\\?\C:\…`
    # (Windows CI 2026-11-15, job 132610759444).
    assert f.is_symlink() and f.resolve() == other.resolve()


# ── преглед на PR #122 (2026-11-15): твърди връзки и прозорецът unlink → запис ──

def _hardlink_or_skip(target: Path, link: Path) -> None:
    try:
        os.link(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("няма твърди връзки на тази файлова система")
    if os.lstat(link).st_nlink < 2:
        pytest.skip("st_nlink не се отчита тук")


def test_hardlink_to_an_outside_file_after_an_edit_is_replaced_not_written(ws, tmp_path) -> None:
    """`rm f && ln /outside/x f` след EDIT: /undo пишеше на място — през общия
    inode — и презаписваше външния файл."""
    shared = tmp_path / "shared.txt"
    shared.write_text("на оператора\n", encoding="utf-8")
    f = ws / "f.txt"

    def swap() -> None:
        f.unlink()
        _hardlink_or_skip(shared, f)

    edit_history.begin_turn("edit, после твърда връзка навън")
    _edit(f, "v2\n")
    _in_command(ws, swap)
    edit_history.end_turn()
    _label, lines = edit_history.undo()
    assert shared.read_text(encoding="utf-8") == "на оператора\n", lines
    assert f.read_text(encoding="utf-8") == "v1\n"
    assert os.lstat(f).st_nlink == 1
    assert any("твърда връзка" in ln for ln in lines), lines


def test_hardlink_a_command_made_is_replaced_not_written(ws, tmp_path) -> None:
    """Същото без EDIT — файлът е само в снимките на командата."""
    shared = tmp_path / "shared.txt"
    shared.write_text("на оператора\n", encoding="utf-8")
    f = ws / "f.txt"

    def swap() -> None:
        f.unlink()
        _hardlink_or_skip(shared, f)

    edit_history.begin_turn("твърда връзка навън от команда")
    _in_command(ws, swap)
    edit_history.end_turn()
    _label, lines = edit_history.undo()
    assert shared.read_text(encoding="utf-8") == "на оператора\n", lines
    assert f.read_text(encoding="utf-8") == "v1\n"


def test_the_operators_own_hardlink_is_restored_in_place(ws, tmp_path) -> None:
    """Твърда връзка отпреди хода е на оператора: EDIT е писал през нея, /undo
    я връща пак през нея (същият inode), не я къса."""
    twin = tmp_path / "twin.txt"
    _hardlink_or_skip(ws / "f.txt", twin)
    f = ws / "f.txt"
    edit_history.begin_turn("edit на файл с твърда връзка")
    _edit(f, "v2\n")
    edit_history.end_turn()
    assert twin.read_text(encoding="utf-8") == "v2\n"
    edit_history.undo()
    assert f.read_text(encoding="utf-8") == twin.read_text(encoding="utf-8") == "v1\n"
    assert os.path.samefile(f, twin)


def test_a_link_put_back_right_after_it_is_removed_is_not_written_through(
        ws, tmp_path, monkeypatch) -> None:
    """Процес, пуснат в хода, слага връзката пак веднага след unlink(): старият
    код пишеше write_bytes() през нея (преглед на PR #122). Тук „процесът“
    печели всяко състезание — сменя всяко unlink на f с нова връзка."""
    shared = tmp_path / "shared.txt"
    shared.write_text("на оператора\n", encoding="utf-8")
    f = ws / "f.txt"

    def swap() -> None:
        f.unlink()
        _symlink_or_skip(shared, f)

    edit_history.begin_turn("edit, после връзка, която се връща")
    _edit(f, "v2\n")
    _in_command(ws, swap)
    edit_history.end_turn()
    real_unlink = Path.unlink

    def racing_unlink(self: Path, missing_ok: bool = False) -> None:
        real_unlink(self, missing_ok=missing_ok)
        if self == f:
            os.symlink(shared, f)
    monkeypatch.setattr(Path, "unlink", racing_unlink)
    _label, lines = edit_history.undo()
    monkeypatch.undo()
    assert shared.read_text(encoding="utf-8") == "на оператора\n", lines
    assert not f.is_symlink() and f.read_text(encoding="utf-8") == "v1\n"


def test_put_back_never_follows_a_link_put_in_after_its_check(tmp_path, monkeypatch) -> None:
    """Прозорецът между lstat и отварянето: O_NOFOLLOW (POSIX) или проверката
    на inode-а след отварянето (Windows) — нов файл и os.replace."""
    f = tmp_path / "f.txt"
    f.write_text("v2\n", encoding="utf-8")
    shared = tmp_path / "shared.txt"
    shared.write_text("общ\n", encoding="utf-8")
    real_lstat = os.lstat
    raced: list[bool] = []

    def racing_lstat(p, *args, **kwargs):
        st = real_lstat(p, *args, **kwargs)
        if not raced and os.fspath(p) == os.fspath(f):
            raced.append(True)
            os.unlink(f)
            _symlink_or_skip(shared, f)
        return st
    monkeypatch.setattr(os, "lstat", racing_lstat)
    edit_history._put_back(f, b"v1\n")
    monkeypatch.undo()
    assert raced
    assert shared.read_text(encoding="utf-8") == "общ\n"
    assert not f.is_symlink() and f.read_bytes() == b"v1\n"


def test_no_temporary_file_is_left_next_to_the_restored_one(ws) -> None:
    f = ws / "f.txt"
    edit_history.begin_turn("edit, после папка на мястото")
    _edit(f, "v2\n")
    _in_command(ws, lambda: (f.unlink(), f.mkdir()))   # replace върху папка не става
    edit_history.end_turn()
    _label, lines = edit_history.undo()
    assert any(ln.startswith("❌") for ln in lines), lines
    assert not [p for p in ws.iterdir() if p.name.startswith(".genesis-undo-")]


@pytest.mark.parametrize(("raw", "git"), [
    ("\\\\?\\C:\\Users\\op\\shared.txt", "C:/Users/op/shared.txt"),
    ("\\??\\C:\\Users\\op\\shared.txt", "C:/Users/op/shared.txt"),
    ("\\\\?\\UNC\\srv\\share\\x.txt", "//srv/share/x.txt"),
    ("..\\lib\\x.py", "../lib/x.py"),
    ("f.sh", "f.sh"),
])
def test_link_target_as_git_for_windows_keeps_it(raw: str, git: str) -> None:
    """Защо два от тестовете по-горе падаха на Windows: снимката пази целта
    като `C:/…`, а os.readlink дава `\\\\?\\C:\\…` — current_id не съвпадаше и
    всяка абсолютна връзка беше „пипната след хода“."""
    from genesis_agent import checkpoints
    assert checkpoints.git_link(raw, windows=True) == git
    assert checkpoints.git_link(raw, windows=False) == raw
