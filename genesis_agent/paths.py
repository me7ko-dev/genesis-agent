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
import re
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


def env_path(name: str, default: Path | str) -> Path:
    """Път от променлива на средата; зададена, но празна = `default`.

    Одит 2026-10-22: `os.environ.get(NAME, default)` даваше `Path("")`, тоест
    „.“, за `GENESIS_HOME=` (`docker run -e GENESIS_HOME` без стойност, ред в
    профила с махната стойност — същото, което config._int_env вече пази).
    Измерено: `genesis setup` записа всички ключове в ./.env на проекта, в
    който стоеше потребителят (неследен файл, лесен за комитване), а
    GENESIS_HISTORY_DIR= и GENESIS_SANDBOX_DIR= правеха историята и
    пясъчника текущата папка."""
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else Path(default)


# User configuration lives here. Override with GENESIS_HOME if you keep
# config elsewhere (e.g. an encrypted volume).
GENESIS_HOME: Path = env_path("GENESIS_HOME", Path.home() / ".genesis")

ENV_FILE: Path = GENESIS_HOME / ".env"

# config.yaml and the starter skills live INSIDE the package. They used to sit
# beside it, which meant an installed copy dropped `config.yaml` and `skills/`
# straight into site-packages, where an unrelated project could import them.
CONFIG_PATH: Path = PACKAGE_DIR / "config.yaml"


class ConfigError(ValueError):
    """config.yaml не става: синтаксисът или формата. Текстът е за човек."""


# Секциите, които кодът чете с `.get(...)`, и списъците от записи в `models:`.
_CONFIG_SECTIONS = {"workspace": "`path:`", "storage": "`history_dir:`",
                    "models": "`default_provider:`, `fallback_models:` …"}
_MODEL_LISTS = ("fallback_models", "coding_models", "light_models", "premium_models")


def _kind(value: object) -> str:
    if isinstance(value, bool):
        return "да/не"
    for kind, name in ((str, "текст"), (list, "списък"), (dict, "речник"), ((int, float), "число")):
        if isinstance(value, kind):
            return name
    return type(value).__name__


def _shown(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "…"


def _yaml_problem(err: Exception) -> str:
    """„ред 14, колона 1: …“ от грешка на YAML парсера (редът е от 1)."""
    mark = getattr(err, "problem_mark", None)
    problem = str(getattr(err, "problem", "") or err).strip()
    where = f"ред {mark.line + 1}, колона {mark.column + 1}: " if mark is not None else ""
    if "\\t" in problem or "\t" in problem:
        problem += " (табулация — YAML иска интервали за отстъп)"
    return where + problem


def load_config(path: Path | str | None = None, *,
                sections: tuple[str, ...] = tuple(_CONFIG_SECTIONS)) -> dict:
    """config.yaml като речник, проверен по форма.

    Одит 2026-10-22: файл, който се парсва, но е с грешна форма (`workspace:
    ~/proj` вместо `workspace:` и `path: ~/proj` под него, списък на върха,
    `models: [1, 2]`), убиваше `genesis`, `genesis -p` и `genesis models` с
    AttributeError и стектрейс, без дума кой ключ е грешен. Вдига ConfigError
    с човешки текст (синтаксис — с реда и колоната); OSError (липсващ файл)
    минава непроменен. `sections` — кои секции да се проверят: веригата на
    Brain чете само `models` и сгрешен `workspace:` не бива да я изпразва за
    `genesis fix` и мисиите, които него не четат."""
    import yaml
    target = Path(path) if path is not None else CONFIG_PATH
    text = target.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError) as e:
        # ValueError: PyYAML строи `2026-13-01` и `!!int abc` направо и вдига
        # гол ValueError, не YAMLError (ревю 2026-10-22: `genesis models` с
        # такъв ред гърмеше със стектрейс).
        raise ConfigError(_yaml_problem(e)) from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"config.yaml трябва да е речник (`ключ: стойност` на всеки ред), "
                          f"а е {_kind(data)} ({_shown(data)})")
    for section in sections:
        value = data.get(section)
        if value is None and section in data:
            # Празна секция = празен речник (ревю 2026-10-22: `models:` без
            # нищо под него минаваше проверката като None и чатът падаше на
            # `config.get("models", {}).get(...)` с AttributeError).
            data[section] = {}
        elif value is not None and not isinstance(value, dict):
            raise ConfigError(f"`{section}` трябва да е речник с {_CONFIG_SECTIONS[section]} на "
                              f"отделен ред под него, а е {_kind(value)} ({_shown(value)})")
    models = data.get("models") if "models" in sections else None
    if not isinstance(models, dict):
        return data
    for group in _MODEL_LISTS:
        entries = models.get(group)
        if entries is None:
            if group in models:
                models[group] = []      # празно `fallback_models:` — същото (TypeError)
            continue
        if not isinstance(entries, list):
            raise ConfigError(f"`models.{group}` трябва да е списък от записи с `provider:` и "
                              f"`model:`, а е {_kind(entries)} ({_shown(entries)})")
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ConfigError(f"`models.{group}[{i}]` трябва да е запис с `provider:` и "
                                  f"`model:`, а е {_kind(entry)} ({_shown(entry)})")
    return data


def config_problem(path: Path | str | None = None, *,
                   sections: tuple[str, ...] = tuple(_CONFIG_SECTIONS)) -> str | None:
    """Какво не е наред с config.yaml, или None — за `genesis doctor` и
    `genesis models`, които иначе показваха празна верига без причина."""
    try:
        load_config(path, sections=sections)
    except ConfigError as e:
        return str(e)
    except (OSError, UnicodeDecodeError) as e:
        return f"не се чете: {e}"
    return None


# Searched in order. The project-local .env wins over the home one, so a
# checkout can be pinned to different keys without touching global config.
ENV_FILES: tuple[str, ...] = (
    str(PROJECT_ROOT / ".env"),
    str(ENV_FILE),
)
# GENESIS_NO_PROJECT_ENV=1 — само GENESIS_HOME/.env. За подпроцеси под тест
# (tests/test_bench_e2e.py), които не бива да виждат ключовете в .env на
# checkout-а: ревю 2026-10-11 — с OPENROUTER_API_KEY там чатът под тест
# пробваше openrouter модели и пращаше истинския ключ към фалшивия шлюз.
if os.environ.get("GENESIS_NO_PROJECT_ENV") == "1":
    ENV_FILES = (str(ENV_FILE),)


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
    if FROZEN or INSTALLED:
        return _frozen_workspace(Path.cwd())
    return PROJECT_ROOT


# pip/pipx copy or the native build — not a git checkout. For those
# PROJECT_ROOT is site-packages (or the install directory): never a workspace.
INSTALLED: bool = FROZEN or PROJECT_ROOT.name in ("site-packages", "dist-packages")


def _frozen_workspace(cwd: Path) -> Path:
    """An installed Genesis works where it was started, like `claude` does —
    the PROJECT_ROOT fallback would be site-packages or the install directory,
    erased by the next update. Measured 2026-09-25 with pipx: `genesis` started
    in a new project asked before every write there ("запис извън workspace")
    and, without a keyboard, declined them all. Except where starting there
    means "started from a shortcut", not "work here": the home directory
    itself, a drive root, the Windows directory, or Genesis's own install
    (the native build's directory, pipx's venv). Those get ~/.genesis/workspace.
    """
    try:
        cwd = cwd.resolve()
    except OSError:
        cwd = Path.home()
    unsafe = {Path.home().resolve(), Path(cwd.anchor)}
    windir = os.environ.get("SystemRoot") or os.environ.get("windir")
    inst = install_dir()
    own_env = sys.prefix if INSTALLED and not FROZEN else None
    inside = [Path(d).resolve() for d in (windir, inst, own_env) if d]
    if cwd in unsafe or any(cwd == d or d in cwd.parents for d in inside):
        fallback = GENESIS_HOME / "workspace"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
    return cwd


_project_python_cache: str | None = None


def project_python(root: Path | str | None = None) -> str:
    """The interpreter that runs the USER'S project (its tests, its scripts).

    In order: the project's own venv (`.venv`/`venv`/`env` in `root`), an
    activated venv (`VIRTUAL_ENV`), then `sys.executable` — unless Genesis
    itself runs from an isolated environment. That is the pipx install (its
    own venv, measured 2026-09-25: `genesis fix` on a real project ran pytest
    there, every test died on `No module named 'reportlab'`, and the model
    spent 8 rounds re-reading files a code edit could never fix) and the
    native build (`sys.executable` is genesis.exe, see
    packaging/genesis_entry.py). Both have none of the project's packages, so
    the real Python on the machine is asked for its own path. The `python` in
    WindowsApps is skipped unprobed: when no Python is installed it is the
    Store stub, which hangs until the timeout instead of failing (measured:
    ~400 s per command, NEXT_STEPS.md).
    """
    global _project_python_cache
    if root is not None:
        for name in (".venv", "venv", "env"):
            own = _venv_python(Path(root) / name)
            if own:
                return own
    active = os.environ.get("VIRTUAL_ENV")
    if active:
        own = _venv_python(Path(active))
        if own:
            return own
    if not FROZEN and not _genesis_is_isolated():
        return sys.executable or "python3"
    if _project_python_cache is None:
        _project_python_cache = _find_system_python() or ""
    if _project_python_cache:
        return _project_python_cache
    return "python" if FROZEN else (sys.executable or "python3")


def _venv_python(venv: Path) -> str | None:
    for exe in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if exe.is_file():
            return str(exe)
    return None


def _genesis_is_isolated() -> bool:
    """Genesis runs from a venv of its own (pipx), not the user's Python."""
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


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
    return _installed_windows_python()


_IS_WINDOWS = os.name == "nt"


def _installed_windows_python() -> str | None:
    """A real python.exe where Windows installers put it — without starting
    any alias. With the Python install manager every `py`/`python`/`python3`
    on PATH is an alias in WindowsApps (measured 2026-09-25 on the operator's
    laptop), indistinguishable from the Store stub, so the loop above finds
    nothing. The PEP 514 registry is checked too, but only a file that exists
    counts: on the same laptop it pointed into a deleted bench_fix temp dir.
    """
    if not _IS_WINDOWS:
        return None
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        def newest_first(pattern: str, *base: str) -> list[Path]:
            found = Path(local, *base).glob(pattern)
            return sorted(found, key=lambda p: [int(n) for n in re.findall(r"\d+", p.parent.name)],
                          reverse=True)
        candidates += newest_first("pythoncore-3*/python.exe", "Python")
        candidates += newest_first("Python3*/python.exe", "Programs", "Python")
    candidates += _registry_pythons()
    for exe in candidates:
        if exe.is_file():
            return str(exe)
    return None


def _registry_pythons() -> list[Path]:
    """ExecutablePath of every PEP 514 PythonCore entry (HKCU, then HKLM)."""
    if sys.platform != "win32":
        return []
    import winreg
    found: list[Path] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            core = winreg.OpenKey(hive, r"Software\Python\PythonCore")
        except OSError:
            continue
        i = 0
        while True:
            try:
                ver = winreg.EnumKey(core, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(core, ver + r"\InstallPath") as k:
                    found.append(Path(winreg.QueryValueEx(k, "ExecutablePath")[0]))
            except OSError:
                continue
    return found


def history_dir() -> Path:
    """Chat history. XDG-correct, so it does not clutter the repo."""
    # Спецификацията на XDG: празна или относителна XDG_DATA_HOME се
    # пренебрегва (одит 2026-10-22: `XDG_DATA_HOME=` даваше относителната
    # „genesis-agent/history“ — история в текущата папка).
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "share"
    return env_path("GENESIS_HISTORY_DIR", base / "genesis-agent" / "history")


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


def env_value(raw: str) -> str:
    """Стойността от ред `KEY=…` в .env: в кавички — точно съдържанието им
    (`"sk-proj #1"` остава с `#`, одит 2026-10-07); без кавички — без коментара
    след интервал."""
    v = raw.strip()
    if len(v) >= 2 and v[0] in "\"'":
        end = v.find(v[0], 1)
        if end != -1:
            return v[1:end]
    return _strip_inline_comment(v).strip('"').strip("'")


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
            # utf-8-sig: .env, записан от PowerShell 5.1, започва с BOM и
            # първият ключ иначе е „\ufeffKEY“ — тоест невидим.
            for line in p.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k.startswith("export "):
                    k = k[len("export "):].strip()
                # Празно `KEY=` не е попадение (одит 2026-10-22): `cp .env.example
                # .env` в checkout-а оставя всеки доставчик като `NAME=`, и
                # ключът от `genesis setup` в ~/.genesis/.env ставаше невидим —
                # get_secret връщаше None, а setup казваше „✅ Записано“.
                # Същото правило като за празна променлива на средата.
                if k == key and env_value(v):
                    return env_value(v)
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


