"""
genesis_agent.paths — where Genesis Agent looks for configuration.

One place, so no module has to guess. Nothing here is tied to a particular
machine or user: every path is derived from the installed package, the user's
home directory, or an environment variable they set themselves.

Precedence when resolving a key (first hit wins):

    1. a real environment variable            (export HF_TOKEN=...)
    2. ./.env next to the project             (per-checkout override)
    3. ~/.genesis/.env                        (the normal place)
    4. config.yaml                            (non-secret defaults only)

Secrets belong in 2 or 3, never in 4 — `config.yaml` is committed, the .env
files are gitignored.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The package itself: .../genesis_agent/
PACKAGE_DIR: Path = Path(__file__).resolve().parent

# True inside the native Windows build (`genesis.exe`, PyInstaller — see
# packaging/). There the package sits in the install directory, which every
# update replaces wholesale: nothing may be WRITTEN relative to PACKAGE_DIR,
# and `sys.executable` is genesis.exe, not a Python interpreter.
FROZEN: bool = bool(getattr(sys, "frozen", False))

# The directory containing the package. For a git checkout this is the repo
# root; for an installed copy it is site-packages.
PROJECT_ROOT: Path = PACKAGE_DIR.parent

# User configuration lives here. Override with GENESIS_HOME if you keep
# config elsewhere (e.g. an encrypted volume).
GENESIS_HOME: Path = Path(os.environ.get("GENESIS_HOME", Path.home() / ".genesis"))

ENV_FILE: Path = GENESIS_HOME / ".env"

# config.yaml and the starter skills live INSIDE the package. They used to sit
# beside it, which meant an installed copy dropped `config.yaml` and `skills/`
# straight into site-packages, where an unrelated project could import them.
CONFIG_PATH: Path = PACKAGE_DIR / "config.yaml"

# Searched in order. The project-local .env wins over the home one, so a
# checkout can be pinned to different keys without touching global config.
ENV_FILES: tuple[str, ...] = (
    str(PROJECT_ROOT / ".env"),
    str(ENV_FILE),
)


def ensure_utf8_streams() -> None:
    """
    A default Windows console is cp1251/cp866, not UTF-8, so the first
    Cyrillic or emoji character any entrypoint prints crashes with
    UnicodeEncodeError — before the user has even seen a prompt. Every
    standalone entrypoint (the `genesis` CLI, `python -m genesis_agent.sandbox`,
    ...) should call this first. No-op on platforms where the streams are
    already UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def install_dir() -> Path | None:
    """The directory holding genesis.exe in the native build, else None."""
    return Path(sys.executable).resolve().parent if FROZEN else None


def workspace_dir() -> Path:
    """
    Where the agent is allowed to work by default. Deliberately NOT the user's
    whole home directory — an agent that starts out pointed at everything is
    one bad command away from a bad day.
    """
    env = os.environ.get("GENESIS_WORKSPACE")
    if env:
        return Path(env)
    if FROZEN:
        return _frozen_workspace(Path.cwd())
    return PROJECT_ROOT


def _frozen_workspace(cwd: Path) -> Path:
    """The native build works where it was started, like `claude` does — the
    PROJECT_ROOT fallback would be the install directory, erased by the next
    update. Except where starting there means "started from a shortcut", not
    "work here": the home directory itself, a drive root, the Windows
    directory, or the install directory. Those get ~/.genesis/workspace.
    """
    try:
        cwd = cwd.resolve()
    except OSError:
        cwd = Path.home()
    unsafe = {Path.home().resolve(), Path(cwd.anchor)}
    windir = os.environ.get("SystemRoot") or os.environ.get("windir")
    inst = install_dir()
    inside = [Path(d).resolve() for d in (windir, inst) if d]
    if cwd in unsafe or any(cwd == d or d in cwd.parents for d in inside):
        fallback = GENESIS_HOME / "workspace"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return cwd


_project_python_cache: str | None = None


def project_python() -> str:
    """The interpreter that runs the USER'S project (its tests, its scripts).

    From pip/pipx that is `sys.executable`. In the native build it is not:
    `sys.executable` is genesis.exe, which can run a plain script (see
    packaging/genesis_entry.py) but has none of the project's packages —
    pytest least of all. There the real Python on the machine is asked for
    its own path. The `python` in WindowsApps is skipped unprobed: when no
    Python is installed it is the Store stub, which hangs until the timeout
    instead of failing (measured: ~400 s per command, NEXT_STEPS.md).
    """
    global _project_python_cache
    if not FROZEN:
        return sys.executable or "python3"
    if _project_python_cache is None:
        _project_python_cache = _find_system_python() or "python"
    return _project_python_cache


def _find_system_python() -> str | None:
    import shutil
    import subprocess

    code = "import sys; print(sys.executable)"
    for argv in (["py", "-3"], ["python"], ["python3"]):
        exe = shutil.which(argv[0])
        if not exe or "windowsapps" in exe.lower():
            continue
        try:
            r = subprocess.run(argv + ["-c", code], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        found = r.stdout.strip().splitlines()[-1:] if r.returncode == 0 else []
        if found and Path(found[0]).is_file():
            return found[0]
    return None


def history_dir() -> Path:
    """Chat history. XDG-correct, so it does not clutter the repo."""
    default = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "genesis-agent" / "history"
    return Path(os.environ.get("GENESIS_HISTORY_DIR", default))


def _strip_inline_comment(value: str) -> str:
    """
    `KEY=value   # note` → `value`.

    Standard .env convention: a `#` only starts a comment when it follows
    whitespace, so a `#` inside an actual secret survives. Without this, a
    trailing note silently becomes part of the key and every request fails
    with a confusing 401.
    """
    for i, ch in enumerate(value):
        if ch == "#" and i > 0 and value[i - 1] in " \t":
            return value[:i].rstrip()
    return value


def read_env_files(key: str) -> str | None:
    """
    Look up `key` in the .env files. Returns None if absent.

    Real environment variables are NOT consulted here — callers check
    `os.environ` first so an explicit `export` always wins.
    """
    for envf in ENV_FILES:
        p = Path(envf)
        if not p.exists():
            continue
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k.startswith("export "):
                    k = k[len("export "):].strip()
                if k == key:
                    return _strip_inline_comment(v.strip()).strip('"').strip("'")
        except OSError:
            continue
    return None


def get_secret(key: str, default: str | None = None) -> str | None:
    """Full precedence chain for one secret."""
    val = os.environ.get(key)
    if val:
        return val
    val = read_env_files(key)
    if val:
        return val
    return default


def ensure_genesis_home() -> Path:
    """Create ~/.genesis with owner-only permissions (it holds API keys)."""
    GENESIS_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    return GENESIS_HOME


