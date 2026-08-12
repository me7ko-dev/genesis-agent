"""genesis_agent/gui/genesis_gui.py + genesis_jarvis.py — the GTK desktop and
voice frontends, previously with zero coverage of any kind.

These two files are deliberately NOT normal package submodules (see
cli.py: `cmd in ("gui", "voice")` loads them with `runpy.run_path`, and
pyproject.toml ships them as raw `gui/*.py` package-data, not importable
`genesis_agent.gui.*` modules meant for `import`). That split is exactly
what let a real bug through: both files imported their sibling module by
bare name (`import gui_sessions`, `from genesis_gui import ...`), which only
resolves when the interpreter itself adds the script's directory to
sys.path — true for `python3 genesis_agent/gui/genesis_gui.py`, false for
`runpy.run_path(...)`, which does NOT modify sys.path. So the actual
installed `genesis gui` / `genesis voice` commands (cli.py's runpy path)
crashed with `ModuleNotFoundError: No module named 'gui_sessions'` on
every invocation; only the manual "run the script directly" fallback ever
worked. Fixed by switching both to package-relative imports
(`from genesis_agent.gui import gui_sessions`); TestLaunchPath below is the
regression test that would have caught it — it drives the exact same
runpy.run_path() call cli.py makes.

GTK/libadwaita import and construct fine with no display connected (verified
manually); only actually *showing* a window needs one, which nothing here does.

PyGObject (`gi`) is a system package (apt python3-gi + GTK4/libadwaita
typelibs), not a pip-installable extra like discord.py — CI does not have it
and getting it there is a separate, riskier change (system library
dependencies, not just a pip install). This file skips gracefully when the
GTK4 stack is absent; it runs locally wherever the desktop app itself would
run.

Note that `import gi` succeeding is NOT enough to run these tests: the
python3-gi binding and the GTK4/libadwaita *typelibs* are separate packages,
and this WSL box has the first without the second. `importorskip("gi")` alone
therefore let the whole file run there, where every test blew up in setup with
`ValueError: Namespace Gtk not available` — 2 failures + 18 errors on a suite
that is otherwise green, i.e. a red run that says nothing about the code. The
guard below asks for what the tests actually need (the Gtk 4.0 namespace) and
skips on anything less.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
except ValueError as exc:  # typelib missing / wrong major version installed
    pytest.skip(f"GTK4 typelibs not installed: {exc}", allow_module_level=True)

GUI_DIR = Path(__file__).resolve().parent.parent / "genesis_agent" / "gui"


class TestLaunchPath:
    """Mirrors genesis_agent/cli.py's `cmd in ("gui", "voice")` branch
    exactly: `runpy.run_path(str(script), run_name="__main__")`. Uses a
    different run_name here only to skip the `if __name__ == "__main__"`
    guard at the bottom (so App().run() doesn't try to open a real window) —
    the import machinery being tested behaves identically either way, since
    sys.path is unaffected by run_name."""

    def test_genesis_gui_loads_without_a_bare_import_error(self) -> None:
        runpy.run_path(str(GUI_DIR / "genesis_gui.py"), run_name="genesis_gui_under_test")

    def test_genesis_jarvis_loads_without_a_bare_import_error(self) -> None:
        runpy.run_path(str(GUI_DIR / "genesis_jarvis.py"), run_name="genesis_jarvis_under_test")


class TestMainDoesNotLeakTheSubcommandIntoArgv:
    """Regression test for a second bug found while manually launching
    `genesis gui`: passing the full sys.argv (`['genesis', 'gui']` via the
    installed command) to Gtk.Application.run() makes GLib treat the
    leftover positional arg "gui" as a file to open — logs a
    GLib-GIO-CRITICAL and the app exits without ever showing a window.
    Neither app parses its own CLI args, so main() must trim to just the
    program name before calling .run()."""

    def test_genesis_gui_main_passes_only_the_program_name(self, monkeypatch, gui_module) -> None:
        monkeypatch.setattr(sys, "argv", ["/usr/bin/genesis", "gui"])
        captured = {}
        monkeypatch.setattr(
            gui_module["App"], "run",
            lambda self, argv: captured.setdefault("argv", argv) or 0,
        )
        gui_module["main"]()
        assert captured["argv"] == ["/usr/bin/genesis"]

    def test_genesis_jarvis_main_passes_only_the_program_name(self, monkeypatch, jarvis_module) -> None:
        monkeypatch.setattr(sys, "argv", ["/usr/bin/genesis", "voice"])
        captured = {}
        monkeypatch.setattr(
            jarvis_module["App"], "run",
            lambda self, argv: captured.setdefault("argv", argv) or 0,
        )
        monkeypatch.setattr(jarvis_module["signal"], "signal", lambda *a: None)
        jarvis_module["main"]()
        assert captured["argv"] == ["/usr/bin/genesis"]


@pytest.fixture(scope="module")
def gui_module():
    return runpy.run_path(str(GUI_DIR / "genesis_gui.py"), run_name="genesis_gui_for_helpers")


@pytest.fixture(scope="module")
def jarvis_module():
    return runpy.run_path(str(GUI_DIR / "genesis_jarvis.py"), run_name="genesis_jarvis_for_helpers")


class TestRelativeTime:
    def test_just_now(self, gui_module) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        assert gui_module["_relative_time"](now) == "току-що"

    def test_minutes_ago(self, gui_module) -> None:
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        assert gui_module["_relative_time"](ts) == "преди 5 мин"

    def test_hours_ago(self, gui_module) -> None:
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        assert gui_module["_relative_time"](ts) == "преди 3 ч"

    def test_yesterday(self, gui_module) -> None:
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(days=1, hours=1)).isoformat()
        assert gui_module["_relative_time"](ts) == "вчера"

    def test_days_ago(self, gui_module) -> None:
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        assert gui_module["_relative_time"](ts) == "преди 3 дни"

    def test_naive_timestamp_treated_as_utc(self, gui_module) -> None:
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        assert gui_module["_relative_time"](ts) == "преди 5 мин"

    def test_invalid_timestamp_returns_empty_string(self, gui_module) -> None:
        assert gui_module["_relative_time"]("not a date") == ""


class TestListWorkspaceFiles:
    def test_lists_files_sorted(self, gui_module, tmp_path) -> None:
        (tmp_path / "b.py").write_text("b")
        (tmp_path / "a.py").write_text("a")
        result = gui_module["_list_workspace_files"](tmp_path)
        assert [p.name for p in result] == ["a.py", "b.py"]

    def test_skips_ignored_directories(self, gui_module, tmp_path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("x")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "x.pyc").write_text("x")
        (tmp_path / "real.py").write_text("x")
        result = gui_module["_list_workspace_files"](tmp_path)
        assert [p.name for p in result] == ["real.py"]

    def test_skips_egg_info_directories(self, gui_module, tmp_path) -> None:
        (tmp_path / "pkg.egg-info").mkdir()
        (tmp_path / "pkg.egg-info" / "PKG-INFO").write_text("x")
        (tmp_path / "real.py").write_text("x")
        result = gui_module["_list_workspace_files"](tmp_path)
        assert [p.name for p in result] == ["real.py"]

    def test_nonexistent_root_returns_empty_list_not_an_exception(self, gui_module, tmp_path) -> None:
        assert gui_module["_list_workspace_files"](tmp_path / "does-not-exist") == []


class TestSpeakableText:
    def test_strips_code_blocks_entirely(self, jarvis_module) -> None:
        text = "Ето отговора:\n```python\nprint('hi')\n```\nГотово."
        out = jarvis_module["_speakable_text"](text)
        assert "print" not in out
        assert "Ето отговора" in out and "Готово" in out

    def test_strips_markdown_emphasis_but_keeps_the_words(self, jarvis_module) -> None:
        out = jarvis_module["_speakable_text"]("Това е **важно** и *друго*.")
        assert "**" not in out and "*" not in out
        assert "важно" in out and "друго" in out

    def test_strips_headers_and_bullets(self, jarvis_module) -> None:
        out = jarvis_module["_speakable_text"]("## Заглавие\n- точка едно\n- точка две")
        assert "#" not in out
        assert "точка едно" in out and "точка две" in out

    def test_urls_become_a_spoken_placeholder(self, jarvis_module) -> None:
        out = jarvis_module["_speakable_text"]("Виж https://example.com/path за детайли.")
        assert "https://" not in out
        assert "линк" in out

    def test_truncated_to_max_chars(self, jarvis_module) -> None:
        out = jarvis_module["_speakable_text"]("x" * 5000)
        assert len(out) <= jarvis_module["TTS_MAX_CHARS"]
