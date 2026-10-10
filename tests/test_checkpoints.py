"""/undo и за командите (2026-10-09): каквото RUN_CMD, умение или MCP
инструмент промени в папката на проекта, се връща — досега /undo казваше
„RUN_CMD не се следи“ и `sed -i`/`rm` оставаха."""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

import genesis_skills as gs
from genesis_agent import checkpoints, edit_history

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="няма git")


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('v1')\n", encoding="utf-8")
    (root / "keep.txt").write_text("остава\n", encoding="utf-8")
    (root / "gone.txt").write_text("ще бъде изтрит\n", encoding="utf-8")
    return root


def _turn_with_command(ws: Path, change) -> None:
    edit_history.begin_turn("команда")
    edit_history.before_command(ws)
    change()
    edit_history.end_turn()


def _command(ws: Path) -> None:
    (ws / "src" / "app.py").write_text("print('v2')\n", encoding="utf-8")
    (ws / "gone.txt").unlink()
    (ws / "build.log").write_text("ново\n", encoding="utf-8")
    (ws / "gen").mkdir()
    (ws / "gen" / "out.py").write_text("x = 1\n", encoding="utf-8")


def test_undo_puts_back_what_a_command_changed(ws) -> None:
    _turn_with_command(ws, lambda: _command(ws))
    plan = edit_history.pending()
    assert any("app.py" in ln and "команда" in ln for ln in plan)
    assert any("build.log" in ln and ln.startswith("изтрий") for ln in plan)
    _label, done = edit_history.undo()
    assert (ws / "src" / "app.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert (ws / "gone.txt").read_text(encoding="utf-8") == "ще бъде изтрит\n"
    assert not (ws / "build.log").exists() and not (ws / "gen" / "out.py").exists()
    assert (ws / "keep.txt").read_text(encoding="utf-8") == "остава\n"
    assert done and edit_history.pending() == []


def test_a_file_changed_after_the_turn_is_left_alone(ws) -> None:
    _turn_with_command(ws, lambda: _command(ws))
    (ws / "src" / "app.py").write_text("print('ръчно')\n", encoding="utf-8")   # операторът
    _label, done = edit_history.undo()
    assert (ws / "src" / "app.py").read_text(encoding="utf-8") == "print('ръчно')\n"
    assert any("app.py" in d and "след хода" in d for d in done)
    assert (ws / "gone.txt").exists()                  # останалото е върнато


def test_an_edit_before_the_command_goes_back_to_before_the_turn(ws) -> None:
    app = ws / "src" / "app.py"
    edit_history.begin_turn("редакция и команда")
    edit_history.record(app)
    app.write_text("print('edit')\n", encoding="utf-8")            # EDIT_FILE
    edit_history.before_command(ws)
    app.write_text("print('command')\n", encoding="utf-8")         # RUN_CMD
    edit_history.end_turn()
    edit_history.undo()
    assert app.read_text(encoding="utf-8") == "print('v1')\n"


def test_bytes_and_line_endings_come_back_exactly(ws) -> None:
    (ws / ".gitattributes").write_text("* text=auto eol=lf\n*.bin filter=lfs\n", encoding="utf-8")
    crlf = ws / "win.txt"
    crlf.write_bytes(b"a\r\nb\r\n")
    blob = ws / "data.bin"
    blob.write_bytes(bytes(range(256)))
    _turn_with_command(ws, lambda: (crlf.write_bytes(b"x\n"), blob.write_bytes(b"")))
    edit_history.undo()
    assert crlf.read_bytes() == b"a\r\nb\r\n"
    assert blob.read_bytes() == bytes(range(256))


def test_node_modules_and_gitignored_files_are_not_tracked(ws) -> None:
    (ws / ".gitignore").write_text("dist/\n", encoding="utf-8")
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "dist").mkdir()

    def install() -> None:
        (ws / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
        (ws / "dist" / "bundle.js").write_text("y", encoding="utf-8")
    _turn_with_command(ws, install)
    assert edit_history.pending() == []
    assert (ws / "node_modules" / "pkg" / "index.js").exists()


@pytest.mark.skipif(os.name == "nt", reason="без изпълними битове на Windows")
def test_the_executable_bit_comes_back(ws) -> None:
    script = ws / "run.sh"
    script.write_text("#!/bin/sh\necho 1\n", encoding="utf-8")
    script.chmod(0o755)
    _turn_with_command(ws, lambda: script.unlink())
    edit_history.undo()
    assert os.access(script, os.X_OK)


def test_the_home_folder_is_not_snapshotted(ws, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: ws))
    _turn_with_command(ws, lambda: _command(ws))
    assert edit_history.pending() == []
    assert "домашната" in edit_history.untracked_note()
    assert not (Path(checkpoints._store(ws))).exists()


def test_undo_says_why_commands_were_not_tracked(ws, monkeypatch) -> None:
    from collections import deque

    from genesis_agent import chat_commands
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: ws))
    _turn_with_command(ws, lambda: _command(ws))
    seen: list[str] = []
    chat_commands.handle("/undo", messages=deque(), workspace=ws, out=seen.append,
                         ask=lambda q: "")
    assert "не са проследени" in seen[0] and "домашната" in seen[0]


def test_the_snapshot_lives_outside_the_project(ws) -> None:
    _turn_with_command(ws, lambda: _command(ws))
    assert not (ws / ".git").exists()
    from genesis_agent import paths
    assert Path(paths.GENESIS_HOME) in checkpoints._store(ws.resolve()).parents


def test_only_tools_that_may_write_take_a_snapshot(ws, monkeypatch) -> None:
    monkeypatch.setattr(gs, "_WORKSPACE", ws)
    taken: list[Path] = []
    monkeypatch.setattr(edit_history, "before_command",
                        lambda ws, at_exec=False: taken.append(ws))
    for name in ("READ_FILE", "SEARCH_CODE", "TODO_WRITE", "WEB_FETCH", "BG_OUTPUT"):
        assert gs._before_tool(name, {"path": "x"}) is None
    assert taken == []
    for name in ("RUN_CMD", "USE_SKILL", "DELEGATE"):
        gs._before_tool(name, {"command": "true"})
    assert taken == [ws, ws, ws]


def test_a_real_run_cmd_is_undone(ws, monkeypatch) -> None:
    # Целият път: RUN_CMD през sandbox-а в чат ход, после /undo.
    monkeypatch.setattr(gs, "_WORKSPACE", ws)
    py = Path(sys.executable).as_posix()
    edit_history.begin_turn("пусни генератора")
    out = gs.dispatch_tool_call("RUN_CMD", {"command": f'"{py}" -c "open(\'made.txt\',\'w\').write(\'1\')"'})
    edit_history.end_turn()
    assert (ws / "made.txt").exists(), out
    edit_history.undo()
    assert not (ws / "made.txt").exists()


# ── одит 2026-10-09 ──────────────────────────────────────────────────────────

def _command_then(ws: Path, change) -> None:
    """Една команда в хода, както я вижда _before_tool/_after_tool."""
    edit_history.before_command(ws)
    change()
    edit_history.after_command()


def test_a_file_gitignore_hid_is_never_deleted(ws) -> None:
    (ws / ".gitignore").write_text("data/\n", encoding="utf-8")
    (ws / "data").mkdir()
    precious = ws / "data" / "customers.csv"
    precious.write_text("единственото копие\n", encoding="utf-8")
    _turn_with_command(ws, lambda: (ws / ".gitignore").write_text("*.log\n", encoding="utf-8"))
    assert not any("customers.csv" in ln and ln.startswith("изтрий") for ln in edit_history.pending())
    edit_history.undo()
    assert precious.read_text(encoding="utf-8") == "единственото копие\n"
    assert (ws / ".gitignore").read_text(encoding="utf-8") == "data/\n"


def test_an_edit_after_a_command_still_goes_back_to_before_the_turn(ws) -> None:
    app = ws / "src" / "app.py"
    edit_history.begin_turn("команда, после редакция")
    _command_then(ws, lambda: app.write_text("print('command')\n", encoding="utf-8"))
    edit_history.record(app)                                   # EDIT_FILE
    app.write_text("print('edit')\n", encoding="utf-8")
    edit_history.forget_if_unchanged(app)
    edit_history.end_turn()
    edit_history.undo()
    assert app.read_text(encoding="utf-8") == "print('v1')\n"


def test_a_file_a_command_created_and_an_edit_changed_is_deleted(ws) -> None:
    gen = ws / "gen.py"
    edit_history.begin_turn("генератор")
    _command_then(ws, lambda: gen.write_text("generated\n", encoding="utf-8"))
    edit_history.record(gen)
    gen.write_text("generated+edited\n", encoding="utf-8")
    edit_history.forget_if_unchanged(gen)
    edit_history.end_turn()
    edit_history.undo()
    assert not gen.exists()


def test_what_the_operator_saved_between_commands_stays(ws) -> None:
    notes = ws / "NOTES.md"
    notes.write_text("old\n", encoding="utf-8")
    app = ws / "src" / "app.py"
    edit_history.begin_turn("две команди")
    _command_then(ws, lambda: app.write_text("print('v2')\n", encoding="utf-8"))
    notes.write_text("ръчно, между командите\n", encoding="utf-8")   # операторът в редактора
    _command_then(ws, lambda: (ws / "keep.txt").write_text("x\n", encoding="utf-8"))
    edit_history.end_turn()
    assert not any("NOTES.md" in ln for ln in edit_history.pending())
    edit_history.undo()
    assert notes.read_text(encoding="utf-8") == "ръчно, между командите\n"
    assert app.read_text(encoding="utf-8") == "print('v1')\n"
    assert (ws / "keep.txt").read_text(encoding="utf-8") == "остава\n"


@pytest.mark.skipif(os.name == "nt", reason="символни връзки на Windows искат права")
def test_undo_never_writes_through_a_link_out_of_the_project(ws, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()

    def swap() -> None:
        shutil.rmtree(ws / "src")
        os.symlink(outside, ws / "src")
    _turn_with_command(ws, swap)
    edit_history.undo()
    assert list(outside.iterdir()) == []


def test_files_over_the_limit_are_left_out_and_the_store_stays_small(ws, monkeypatch) -> None:
    monkeypatch.setattr(checkpoints, "MAX_FILE_MB", 0)       # всеки непразен файл е „голям“
    (ws / "empty.txt").write_text("", encoding="utf-8")
    _turn_with_command(ws, lambda: (ws / "src" / "app.py").write_text("x\n", encoding="utf-8"))
    assert edit_history.pending() == []                      # app.py е извън снимката
    assert (ws / "src" / "app.py").read_text(encoding="utf-8") == "x\n"


def test_a_tree_over_the_size_limit_is_not_snapshotted(ws, monkeypatch) -> None:
    monkeypatch.setattr(checkpoints, "MAX_TOTAL_MB", 0)
    _turn_with_command(ws, lambda: _command(ws))
    assert edit_history.pending() == []
    assert "MB" in edit_history.untracked_note()


def test_a_stale_index_lock_does_not_break_the_folder_for_good(ws, monkeypatch) -> None:
    _turn_with_command(ws, lambda: None)                     # хранилището вече е създадено
    lock = checkpoints._store(ws.resolve()) / "index.lock"
    lock.write_text("", encoding="utf-8")
    old = time.time() - 10 * checkpoints._TIMEOUT
    os.utime(lock, (old, old))
    _turn_with_command(ws, lambda: _command(ws))
    assert any("app.py" in ln for ln in edit_history.pending())


def test_nested_build_and_vendor_folders_are_tracked(ws) -> None:
    (ws / "pkg" / "build").mkdir(parents=True)
    builder = ws / "pkg" / "build" / "builder.py"
    builder.write_text("v1\n", encoding="utf-8")
    _turn_with_command(ws, lambda: builder.write_text("v2\n", encoding="utf-8"))
    edit_history.undo()
    assert builder.read_text(encoding="utf-8") == "v1\n"


def test_old_versions_are_pruned_once_per_process(ws, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    real = checkpoints._git

    def spy(workspace, *args):
        calls.append(args)
        return real(workspace, *args)
    monkeypatch.setattr(checkpoints, "_git", spy)
    monkeypatch.setattr(checkpoints, "_pruned", set())
    _turn_with_command(ws, lambda: _command(ws))
    _turn_with_command(ws, lambda: None)
    assert sum(1 for c in calls if c[:1] == ("prune",)) == 1


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="root чете всичко; на Windows няма chmod 000")
def test_a_file_git_could_not_read_is_never_deleted(ws) -> None:
    ledger = ws / "ledger.csv"
    ledger.write_text("единственото копие\n", encoding="utf-8")
    ledger.chmod(0)
    try:
        _turn_with_command(ws, lambda: ledger.chmod(0o644))
        edit_history.undo()
        assert ledger.exists()
    finally:
        ledger.chmod(0o644)


# ── втори кръг одит 2026-10-09 ───────────────────────────────────────────────

def test_a_formatter_hook_after_an_edit_does_not_stop_undo(ws, monkeypatch) -> None:
    import json

    from genesis_agent import paths
    monkeypatch.setattr(gs, "_WORKSPACE", ws)
    py = Path(sys.executable).as_posix()
    hook = f'"{py}" -c "import os;open(os.environ[\'GENESIS_FILE\'],\'a\').write(\'# fmt\\n\')"'
    (Path(paths.GENESIS_HOME) / "hooks.json").write_text(json.dumps(
        {"PostToolUse": [{"matcher": "EDIT_FILE", "command": hook}]}), encoding="utf-8")
    app = ws / "src" / "app.py"
    edit_history.begin_turn("редакция")
    gs.dispatch_tool_call("READ_FILE", {"path": str(app)})
    gs.dispatch_tool_call("EDIT_FILE", {"path": str(app), "old": "v1", "new": "edit"})
    edit_history.end_turn()
    assert app.read_text(encoding="utf-8").endswith("# fmt\n")      # hook-ът мина
    edit_history.undo()
    assert app.read_text(encoding="utf-8") == "print('v1')\n"


def test_a_failed_snapshot_after_a_command_never_undoes_the_turn_before(ws, monkeypatch) -> None:
    app = ws / "src" / "app.py"
    edit_history.begin_turn("ход 1")
    edit_history.record(ws / "keep.txt")
    (ws / "keep.txt").write_text("ход 1\n", encoding="utf-8")
    edit_history.end_turn()
    real = checkpoints.snapshot
    calls = {"n": 0}

    def flaky(workspace, big=None):
        calls["n"] += 1
        return real(workspace, big) if calls["n"] == 1 else None   # след командата — не
    monkeypatch.setattr(checkpoints, "snapshot", flaky)
    _turn_with_command(ws, lambda: app.write_text("print('v2')\n", encoding="utf-8"))
    assert any("не може" in ln for ln in edit_history.pending())
    label, done = edit_history.undo()
    assert label == "команда" and any("❌" in d for d in done)
    assert (ws / "keep.txt").read_text(encoding="utf-8") == "ход 1\n"   # ход 1 — непипнат


def test_a_file_that_grew_over_the_limit_leaves_the_store(ws, monkeypatch) -> None:
    db = ws / "app.db"
    db.write_bytes(b"x" * 100)
    _turn_with_command(ws, lambda: None)                         # app.db — в index-а
    monkeypatch.setattr(checkpoints, "MAX_FILE_MB", 0)            # вече е „голям“
    grown = b"y" * 5000
    _turn_with_command(ws, lambda: db.write_bytes(grown))
    with pytest.raises(OSError):
        checkpoints._git(ws.resolve(), "cat-file", "-e", checkpoints.blob_id(grown))
    edit_history.undo()
    assert db.read_bytes() == grown                              # голям — не се връща


def test_a_big_file_a_command_creates_is_not_stored(ws, monkeypatch) -> None:
    monkeypatch.setattr(checkpoints, "MAX_FILE_MB", 0)
    data = b"z" * 4000
    _turn_with_command(ws, lambda: (ws / "out.bin").write_bytes(data))
    with pytest.raises(OSError):
        checkpoints._git(ws.resolve(), "cat-file", "-e", checkpoints.blob_id(data))


@pytest.mark.skipif(os.name == "nt", reason="без изпълними битове на Windows")
def test_a_mode_only_change_is_undone(ws) -> None:
    script = ws / "run.sh"
    script.write_text("echo 1\n", encoding="utf-8")
    script.chmod(0o644)
    _turn_with_command(ws, lambda: script.chmod(0o755))
    edit_history.undo()
    assert not os.access(script, os.X_OK)


def test_another_processs_prune_keeps_what_this_one_needs(ws) -> None:
    _turn_with_command(ws, lambda: _command(ws))
    checkpoints._git(ws.resolve(), "prune", "--expire=now")      # най-лошото от друг процес
    edit_history.undo()
    assert (ws / "src" / "app.py").read_text(encoding="utf-8") == "print('v1')\n"
    assert (ws / "gone.txt").exists()


@pytest.mark.skipif(os.name == "nt", reason="rm -rf през bash")
def test_what_the_operator_saves_while_approving_stays(ws, monkeypatch) -> None:
    from genesis_agent import sandbox
    monkeypatch.setattr(gs, "_WORKSPACE", ws)
    notes = ws / "NOTES.md"
    notes.write_text("old\n", encoding="utf-8")
    (ws / "out").mkdir()
    (ws / "out" / "x.txt").write_text("x", encoding="utf-8")

    def approve(_op, _verdict) -> bool:
        notes.write_text("записано, докато питаше\n", encoding="utf-8")   # операторът
        return True
    monkeypatch.setattr(sandbox, "_POLICY", sandbox.SandboxPolicy(mode="interactive",
                                                                 confirm_fn=approve))
    edit_history.begin_turn("чистене")
    gs.dispatch_tool_call("RUN_CMD", {"command": "rm -rf out"})
    edit_history.end_turn()
    assert not (ws / "out").exists()
    edit_history.undo()
    assert notes.read_text(encoding="utf-8") == "записано, докато питаше\n"
    assert (ws / "out" / "x.txt").read_text(encoding="utf-8") == "x"


def test_writes_inside_delegate_outside_the_snapshot_are_undone(ws, tmp_path) -> None:
    (ws / ".gitignore").write_text("local.py\n", encoding="utf-8")
    local = ws / "local.py"
    local.write_text("SECRET = 1\n", encoding="utf-8")
    other = tmp_path / "other.md"
    other.write_text("извън\n", encoding="utf-8")
    edit_history.begin_turn("делегирано")
    edit_history.before_command(ws)                               # DELEGATE
    for f in (local, other, ws / "src" / "app.py"):
        edit_history.record(f)
        f.write_text("променен\n", encoding="utf-8")
        edit_history.forget_if_unchanged(f)
    edit_history.after_command()
    edit_history.end_turn()
    edit_history.undo()
    assert local.read_text(encoding="utf-8") == "SECRET = 1\n"
    assert other.read_text(encoding="utf-8") == "извън\n"
    assert (ws / "src" / "app.py").read_text(encoding="utf-8") == "print('v1')\n"


@pytest.mark.skipif(os.name == "nt", reason="символни връзки на Windows искат права")
def test_undo_of_an_edit_never_writes_through_a_link_out(ws, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    app = ws / "src" / "app.py"
    edit_history.begin_turn("редакция, после връзка")
    edit_history.record(app)
    app.write_text("print('edit')\n", encoding="utf-8")
    edit_history.forget_if_unchanged(app)

    def swap() -> None:
        (ws / "src").rename(ws / "src_old")
        os.symlink(outside, ws / "src")
    edit_history.before_command(ws)
    swap()
    edit_history.after_command()
    edit_history.end_turn()
    edit_history.undo()
    assert list(outside.iterdir()) == []


# ── трети одит 2026-10-09 ────────────────────────────────────────────────────

def test_a_background_command_never_makes_undo_hit_the_turn_before(ws, monkeypatch) -> None:
    from genesis_agent import background
    monkeypatch.setattr(gs, "_WORKSPACE", ws)
    edit_history.begin_turn("ход 1")
    edit_history.record(ws / "keep.txt")
    (ws / "keep.txt").write_text("ход 1\n", encoding="utf-8")
    edit_history.end_turn()
    py = Path(sys.executable).as_posix()
    edit_history.begin_turn("сървър")
    out = gs.dispatch_tool_call("RUN_CMD", {"command": f'"{py}" -c "import time; time.sleep(5)"',
                                            "background": True})
    edit_history.end_turn()
    try:
        assert "bg" in out, out
        label, done = edit_history.undo()
        assert label == "сървър" and any("фонова" in d for d in done)
        assert (ws / "keep.txt").read_text(encoding="utf-8") == "ход 1\n"
    finally:
        background.stop_all()


def test_a_refused_background_command_takes_no_undo_step(ws, monkeypatch) -> None:
    monkeypatch.setattr(gs, "_WORKSPACE", ws)
    edit_history.begin_turn("ход 1")
    edit_history.record(ws / "keep.txt")
    (ws / "keep.txt").write_text("ход 1\n", encoding="utf-8")
    edit_history.end_turn()
    edit_history.begin_turn("отказан сървър")
    out = gs.dispatch_tool_call("RUN_CMD", {"command": "sudo python3 -m http.server",
                                            "background": True})
    edit_history.end_turn()
    assert "SANDBOX" in out
    label, _ = edit_history.undo()
    assert label == "ход 1"


def test_a_big_file_a_command_creates_is_deleted_by_undo(ws, monkeypatch) -> None:
    monkeypatch.setattr(checkpoints, "MAX_FILE_MB", 0)
    (ws / "empty.txt").write_text("", encoding="utf-8")
    model = ws / "model.bin"
    _turn_with_command(ws, lambda: model.write_bytes(b"w" * 5000))
    assert any("model.bin" in ln and ln.startswith("изтрий") for ln in edit_history.pending())
    edit_history.undo()
    assert not model.exists()


def test_a_big_write_inside_delegate_is_undone(ws, monkeypatch) -> None:
    monkeypatch.setattr(edit_history, "_MAX_BYTES", 10)
    data = ws / "data.json"
    data.write_text('{"a": [1, 2, 3, 4, 5]}', encoding="utf-8")
    edit_history.begin_turn("делегирано")
    edit_history.before_command(ws)                               # DELEGATE
    edit_history.record(data)                                     # → too_big (над 10 байта)
    data.write_text("[]", encoding="utf-8")
    edit_history.after_command()
    edit_history.end_turn()
    edit_history.undo()
    assert data.read_text(encoding="utf-8") == '{"a": [1, 2, 3, 4, 5]}'


def test_a_command_that_crosses_the_file_limit_still_undoes_tracked_files(ws, monkeypatch) -> None:
    app = ws / "src" / "app.py"

    def flood() -> None:
        monkeypatch.setattr(checkpoints, "MAX_FILES", 10)       # „над границата“ след командата
        app.write_text("print('v2')\n", encoding="utf-8")
        (ws / "data").mkdir()
        for i in range(30):
            (ws / "data" / f"{i}.txt").write_text("x", encoding="utf-8")
    _turn_with_command(ws, flood)
    edit_history.undo()
    assert app.read_text(encoding="utf-8") == "print('v1')\n"


def test_another_process_in_another_pid_namespace_keeps_its_refs(ws) -> None:
    _turn_with_command(ws, lambda: None)
    ws_r = ws.resolve()
    store = checkpoints._store(ws_r)
    tree = checkpoints._git(ws_r, "write-tree").decode().strip()
    other = "1-abcdef"                                            # pid 1 в друг контейнер
    checkpoints._git(ws_r, "update-ref", f"refs/genesis/{other}/1", tree)
    (store / f"index-{other}").write_bytes(b"")                   # пипнат току-що
    checkpoints._cleanup(ws_r, store)
    refs = checkpoints._git(ws_r, "for-each-ref", "--format=%(refname)", "refs/genesis/").decode()
    assert f"refs/genesis/{other}/1" in refs
    old = time.time() - 2 * 24 * 3600                             # мълчи от два дни
    os.utime(store / f"index-{other}", (old, old))
    checkpoints._cleanup(ws_r, store)
    refs = checkpoints._git(ws_r, "for-each-ref", "--format=%(refname)", "refs/genesis/").decode()
    assert f"refs/genesis/{other}/1" not in refs


def test_a_new_process_starts_from_the_newest_index(ws, monkeypatch) -> None:
    _turn_with_command(ws, lambda: None)
    store = checkpoints._store(ws.resolve())
    monkeypatch.setattr(checkpoints, "_TAG", "99999-fresh0")
    monkeypatch.setattr(checkpoints, "_pruned", set())
    _turn_with_command(ws, lambda: None)
    assert (store / "index-99999-fresh0").stat().st_size > 0
