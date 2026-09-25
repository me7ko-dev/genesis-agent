"""The native Windows build (genesis.exe): the parts that can be checked
without building it. packaging/build.py smoke-tests the built binary itself,
and .github/workflows/native.yml runs the installer against it on Windows."""
from __future__ import annotations

import importlib.util
import json
import struct
import sys
from pathlib import Path

import pytest

from genesis_agent import paths, self_update, version_info

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entry = _load("genesis_entry", ROOT / "packaging" / "genesis_entry.py")
build = _load("genesis_build", ROOT / "packaging" / "build.py")


# ── genesis.exe as a Python interpreter ─────────────────────────────────────

@pytest.mark.parametrize("argv, expected", [
    (["run_ab12.py"], ("path", "run_ab12.py", [])),
    (["C:\\sandbox\\run.py", "a", "b"], ("path", "C:\\sandbox\\run.py", ["a", "b"])),
    (["-c", "print(1)", "x"], ("c", "print(1)", ["x"])),
    (["-m", "pytest", "-q"], ("m", "pytest", ["-q"])),
    (["-u", "-X", "utf8", "script.py"], ("path", "script.py", [])),
    (["-I", "-m", "json.tool"], ("m", "json.tool", [])),
])
def test_python_style_argv_runs_python(argv, expected) -> None:
    assert entry.python_invocation(argv) == expected


@pytest.mark.parametrize("argv", [
    [], ["setup"], ["fix", "proj/app.py", "bug"], ["mission", "write x.py"],
    ["--version"], ["-c"], ["-m"], ["-u"], ["models", "--check"],
])
def test_genesis_commands_are_not_python(argv) -> None:
    """A subcommand that merely mentions a .py file later is still the CLI."""
    assert entry.python_invocation(argv) is None


def test_python_mode_runs_a_script_with_its_argv(tmp_path, capsys, monkeypatch) -> None:
    script = tmp_path / "s.py"
    (tmp_path / "native_entry_helper.py").write_text("VALUE = 42\n")
    script.write_text("import sys, native_entry_helper as h\nprint(h.VALUE, sys.argv[1:], __name__)\n")
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert entry.run_python("path", str(script), ["a"]) == 0
    assert "42 ['a'] __main__" in capsys.readouterr().out


def test_python_mode_reports_an_exception_like_python(capsys, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert entry.run_python("c", "raise ValueError('boom')", []) == 1
    assert "ValueError: boom" in capsys.readouterr().err


def test_python_mode_keeps_the_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(SystemExit) as exc:
        entry.run_python("c", "import sys; sys.exit(3)", [])
    assert exc.value.code == 3


# ── where the frozen app works and which Python tests a project ────────────

def test_frozen_workspace_is_where_it_was_started(tmp_path, monkeypatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setattr(paths, "GENESIS_HOME", tmp_path / "gh")
    assert paths._frozen_workspace(project) == project.resolve()


def test_frozen_workspace_is_not_the_home_directory(tmp_path, monkeypatch) -> None:
    """Started from the Start menu or a fresh terminal = home. The agent does
    not get the whole home directory as its playground."""
    monkeypatch.setattr(paths, "GENESIS_HOME", tmp_path / "gh")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert paths._frozen_workspace(tmp_path) == tmp_path / "gh" / "workspace"
    assert (tmp_path / "gh" / "workspace").is_dir()


def test_frozen_workspace_is_never_the_install_directory(tmp_path, monkeypatch) -> None:
    """Every update replaces the install directory wholesale."""
    install = tmp_path / "Programs" / "Genesis"
    (install / "_internal").mkdir(parents=True)
    monkeypatch.setattr(paths, "GENESIS_HOME", tmp_path / "gh")
    monkeypatch.setattr(paths, "install_dir", lambda: install)
    assert paths._frozen_workspace(install / "_internal") == tmp_path / "gh" / "workspace"


def test_pipx_genesis_works_where_it_was_started(tmp_path, monkeypatch) -> None:
    """pipx: PROJECT_ROOT is site-packages. `genesis` started in a project
    asked before every write there (2026-09-25) — the project is the workspace."""
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.delenv("GENESIS_WORKSPACE", raising=False)
    monkeypatch.setattr(paths, "FROZEN", False)
    monkeypatch.setattr(paths, "INSTALLED", True)
    monkeypatch.setattr(paths, "GENESIS_HOME", tmp_path / "gh")
    monkeypatch.chdir(project)
    assert paths.workspace_dir() == project.resolve()


def test_pipx_genesis_never_works_inside_its_own_venv(tmp_path, monkeypatch) -> None:
    venv = tmp_path / "pipx" / "venvs" / "genesis-agent"
    (venv / "Lib").mkdir(parents=True)
    monkeypatch.setattr(paths, "FROZEN", False)
    monkeypatch.setattr(paths, "INSTALLED", True)
    monkeypatch.setattr(paths.sys, "prefix", str(venv))
    monkeypatch.setattr(paths, "GENESIS_HOME", tmp_path / "gh")
    assert paths._frozen_workspace(venv / "Lib") == tmp_path / "gh" / "workspace"


def test_a_checkout_keeps_the_repo_as_workspace(monkeypatch) -> None:
    monkeypatch.delenv("GENESIS_WORKSPACE", raising=False)
    monkeypatch.setattr(paths, "FROZEN", False)
    monkeypatch.setattr(paths, "INSTALLED", False)
    assert paths.workspace_dir() == paths.PROJECT_ROOT


def test_project_python_outside_the_build_is_this_interpreter(monkeypatch) -> None:
    monkeypatch.setattr(paths, "FROZEN", False)
    monkeypatch.setattr(paths, "_genesis_is_isolated", lambda: False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert paths.project_python() == sys.executable


def test_project_python_in_the_build_asks_the_real_python(monkeypatch, tmp_path) -> None:
    real = tmp_path / "python.exe"
    real.write_text("")
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths, "_project_python_cache", None)
    monkeypatch.setattr(paths, "_find_system_python", lambda: str(real))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert paths.project_python() == str(real)


def test_project_python_from_pipx_asks_the_real_python(monkeypatch, tmp_path) -> None:
    """pipx: Genesis has a venv of its own, without the project's packages
    (2026-09-25: every test died on `No module named 'reportlab'`)."""
    real = tmp_path / "python.exe"
    real.write_text("")
    monkeypatch.setattr(paths, "FROZEN", False)
    monkeypatch.setattr(paths, "_genesis_is_isolated", lambda: True)
    monkeypatch.setattr(paths, "_project_python_cache", None)
    monkeypatch.setattr(paths, "_find_system_python", lambda: str(real))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert paths.project_python(tmp_path) == str(real)


def test_project_python_from_pipx_without_a_system_python_keeps_its_own(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(paths, "FROZEN", False)
    monkeypatch.setattr(paths, "_genesis_is_isolated", lambda: True)
    monkeypatch.setattr(paths, "_project_python_cache", None)
    monkeypatch.setattr(paths, "_find_system_python", lambda: None)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert paths.project_python(tmp_path) == sys.executable


@pytest.mark.parametrize("layout", [("Scripts", "python.exe"), ("bin", "python")])
def test_the_projects_own_venv_comes_first(monkeypatch, tmp_path, layout) -> None:
    own = tmp_path / ".venv" / layout[0] / layout[1]
    own.parent.mkdir(parents=True)
    own.write_text("")
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths, "_find_system_python", lambda: "C:/other/python.exe")
    assert paths.project_python(tmp_path) == str(own)


def test_an_activated_venv_comes_before_the_system_python(monkeypatch, tmp_path) -> None:
    active = tmp_path / "work-env"
    (active / "Scripts").mkdir(parents=True)
    (active / "Scripts" / "python.exe").write_text("")
    monkeypatch.setenv("VIRTUAL_ENV", str(active))
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths, "_find_system_python", lambda: "C:/other/python.exe")
    assert paths.project_python(tmp_path / "project") == str(active / "Scripts" / "python.exe")


def test_the_store_stub_is_never_probed(monkeypatch) -> None:
    """With no Python installed, WindowsApps\\python.exe hangs until the
    timeout instead of failing — it must not even be started."""
    import shutil
    import subprocess
    monkeypatch.setattr(shutil, "which",
                        lambda n: rf"C:\Users\x\AppData\Local\Microsoft\WindowsApps\{n}.exe")
    started: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **_k: started.append(argv))
    monkeypatch.setattr(paths, "_installed_windows_python", lambda: None)
    assert paths._find_system_python() is None
    assert started == []


def test_with_only_aliases_the_installed_python_is_found(monkeypatch, tmp_path) -> None:
    """Python install manager: every py/python/python3 is a WindowsApps alias
    (the operator's laptop, 2026-09-25). The newest pythoncore wins, by version
    number, not by string (3.14 > 3.9)."""
    import shutil
    monkeypatch.setattr(shutil, "which",
                        lambda n: rf"C:\Users\x\AppData\Local\Microsoft\WindowsApps\{n}.exe")
    monkeypatch.setattr(paths, "_IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for ver in ("pythoncore-3.9-64", "pythoncore-3.14-64"):
        (tmp_path / "Python" / ver).mkdir(parents=True)
        (tmp_path / "Python" / ver / "python.exe").write_text("")
    assert paths._find_system_python() == str(tmp_path / "Python" / "pythoncore-3.14-64" / "python.exe")


def test_repo_map_tests_a_project_with_the_project_python(tmp_path, monkeypatch) -> None:
    from genesis_agent import repo_map
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setattr("genesis_agent.paths.project_python", lambda root=None: r"C:\Py312\python.exe")
    assert repo_map.detect_project(tmp_path).test_command == "C:/Py312/python.exe -m pytest -q"


# ── which version is installed and how it updates ──────────────────────────

def test_frozen_version_comes_from_build_info(tmp_path, monkeypatch) -> None:
    (tmp_path / "_build_info.json").write_text(json.dumps(
        {"url": "https://github.com/me7ko-dev/genesis-agent", "commit": "abc1234def", "ref": "latest"}))
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths, "PACKAGE_DIR", tmp_path)
    src = version_info.installed_source()
    assert src is not None and src.commit == "abc1234def" and src.ref == "latest"
    assert version_info.describe("0.2.0") == "genesis-agent 0.2.0 (abc1234, latest)"
    assert "install.ps1 | iex" in version_info.install_command(src)


def test_frozen_without_build_info_is_not_an_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths, "PACKAGE_DIR", tmp_path)
    assert version_info.installed_source() is None


def test_latest_release_is_compared_by_its_commit(monkeypatch) -> None:
    src = version_info.Source(url="https://github.com/o/r", commit="old", ref="latest")
    monkeypatch.setattr(version_info, "installed_source", lambda: src)
    monkeypatch.setattr(version_info, "latest_release_commit", lambda repo, timeout: "new")
    monkeypatch.setattr(version_info, "latest_commit",
                        lambda *a, **k: pytest.fail("a release is not a branch"))
    check = version_info.check_update()
    assert check.latest == "new" and check.up_to_date is False


def test_native_update_runs_the_installer_after_exit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(self_update, "GENESIS_HOME", tmp_path)
    argv = self_update.native_update_argv(pid=4242, ref="latest", owner_repo="o/r",
                                          script=tmp_path / "u.ps1",
                                          install_dir=tmp_path / "Genesis")
    joined = " ".join(argv)
    assert argv[0] == "powershell.exe" and "-File" in argv
    assert "-WaitPid 4242" in joined and "-Tag latest" in joined and "-Repo o/r" in joined
    assert str(tmp_path / "update_state.json") in argv
    # Never a prompt in the hidden window: -WaitPid implies no questions,
    # and the user's Start menu entry is left alone.
    assert "-NoShortcut" in argv
    # The chat can go on for hours after /update; the updater keeps waiting.
    assert int(argv[argv.index("-WaitTimeout") + 1]) >= 3600


def test_request_update_in_the_build_takes_the_native_path(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(self_update, "request_native_update", lambda **k: calls.append(k))
    monkeypatch.setattr(self_update.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("pipx path taken in the native build"))
    self_update.request_update(pid=1, url="https://github.com/o/r", ref="latest")
    assert calls == [{"pid": 1, "url": "https://github.com/o/r", "ref": "latest"}]


# ── the build itself ───────────────────────────────────────────────────────

def test_icon_is_a_valid_ico_with_png_images(tmp_path) -> None:
    ico = tmp_path / "g.ico"
    build.write_icon(ico)
    data = ico.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, 4)
    for i in range(count):
        _w, _h, _c, _r, _p, _b, size, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i: 22 + 16 * i])
        assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert offset + size <= len(data)


def test_the_spec_ships_what_pip_ships() -> None:
    """[tool.setuptools.package-data] and genesis.spec must not drift: a file
    only one of them ships works from one install and not the other."""
    spec = (ROOT / "packaging" / "genesis.spec").read_text(encoding="utf-8")
    for needed in ('"config.yaml"', '"skills"', '"skills.json"', '"install.ps1"',
                   "genesis_terminal_agent", "genesis_skills"):
        assert needed in spec


def test_installer_and_app_agree_on_the_asset_name() -> None:
    ps1 = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "native.yml").read_text(encoding="utf-8")
    assert '$GenesisAsset = "genesis-windows-x64.zip"' in ps1
    assert "genesis-windows-x64.zip" in workflow
    assert build.DEFAULT_TAG == version_info.LATEST_RELEASE
    assert '[string]$Tag = "latest"' in ps1


def test_installer_is_ascii() -> None:
    """PowerShell 5.1 reads a BOM-less .ps1 in the system codepage."""
    (ROOT / "scripts" / "install.ps1").read_bytes().decode("ascii")
