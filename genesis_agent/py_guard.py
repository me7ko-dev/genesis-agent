"""
genesis_agent.py_guard — стартов скрипт за Python код от модела (2026-10-09).

    python py_guard.py <script.py>

Слага `sys.addaudithook`, който отказва отваряне, листване, промяна и пускане
на процес към тайните — по ИСТИНСКИЯ път, без значение как е сглобен — и пуска
скрипта с runpy. Защо: sandbox.assess_code вижда само буквални пътища в кода.
`Path.home().parents[1] / ".ssh"`, `pwd.getpwuid()`, `"~/.s" + "sh"` минаваха
(одит 2026-10-09, възпроизведено). Hook-ът не може да бъде махнат от кода след
това (sys.addaudithook е за целия процес).

Одит на пазача (2026-10-09), затворено:
  * процеси: без одобрение — никакви (дете е Python без hook); и multiprocessing
    („spawn“ вика _posixsubprocess направо, без събитие);
  * `os.open(..., dir_fd=…)` и listdir по fd — относителни имена от чужда папка;
  * sqlite3 (memory.db, „Login Data“) — отваря се в C;
  * запис в site-packages/user site: `.pth` там тръгва преди hook-а при
    следващото пускане (и в самия Genesis);
  * папката на скрипта е нова за всяко пускане — подхвърлен `runpy.py` (3.10).
Известно, не е затворено: ctypes вика libc направо; os.stat казва, че файлът
съществува и колко е голям (няма събитие). Стена е само отделен потребител.

Средата (от sandbox.run_python):
  GENESIS_GUARD_RX      — регексът на тайните
  GENESIS_GUARD_EXEMPT  — изключенията (.env.example, …)
  GENESIS_GUARD_BLOCK   — пътища изцяло забранени (тайните в ~/.genesis), os.pathsep
  GENESIS_GUARD_ALLOW   — папки извън BLOCK правилото (работната папка)
  GENESIS_GUARD_PROCS   — "1": операторът е одобрил пускане на процеси
"""
from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable

_EVENTS_READ = {"open", "os.listdir", "os.scandir", "os.chdir", "sqlite3.connect",
                "shutil.copyfile", "shutil.copytree"}
_EVENTS_WRITE = {"os.remove", "os.rename", "os.replace", "os.rmdir", "shutil.rmtree",
                 "os.symlink", "os.link", "os.truncate", "os.utime", "os.chmod", "os.chown",
                 "os.mkdir"}
_EVENTS_PROC = {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                "os.fork", "os.forkpty", "os.startfile", "pty.spawn"}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC


def _install() -> None:
    import re
    rx = re.compile(os.environ.get("GENESIS_GUARD_RX") or r"(?!x)x", re.IGNORECASE)
    exempt = re.compile(os.environ.get("GENESIS_GUARD_EXEMPT") or r"(?!x)x", re.IGNORECASE)
    procs = os.environ.get("GENESIS_GUARD_PROCS") == "1"

    def real(p: str) -> str:
        try:
            return os.path.realpath(p)
        except (OSError, ValueError):
            return p

    def paths(name: str) -> list[str]:
        return [real(d) for d in (os.environ.get(name) or "").split(os.pathsep) if d]

    blocked, allowed = paths("GENESIS_GUARD_BLOCK"), paths("GENESIS_GUARD_ALLOW")
    # Прочетено — вън от средата: скриптът и децата му не виждат настройките
    # на пазача (и средата остава шепа променливи — Windows CI 2026-10-09).
    for name in [k for k in os.environ if k.startswith("GENESIS_GUARD_")]:
        del os.environ[name]
    import site
    libs = {real(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    try:
        libs.update(real(p) for p in site.getsitepackages())
        libs.add(real(site.getusersitepackages()))
    except Exception:
        pass

    def under(path: str, roots: list[str] | set[str]) -> bool:
        return any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) for r in roots)

    def secret(text: str) -> bool:
        flat = text.replace("\\", "/")
        return bool(rx.search(flat)) and not exempt.search(flat)

    def check(raw: object, write: bool, event: str = "") -> None:
        if isinstance(raw, int):
            if event == "open":
                # open(fd)/os.fdopen: вече отворен дескриптор (mkstemp…) — самото
                # отваряне е минало през проверката. На Windows без /proc това
                # спираше всяко fdopen и всеки subprocess (CI 2026-10-09).
                return
            # listdir/scandir/chdir по отворена папка: какво е тя (Linux) — иначе не.
            try:
                raw = os.readlink(f"/proc/self/fd/{raw}")
            except OSError:
                raise PermissionError("[SANDBOX] достъп по файлов дескриптор отказан") from None
        if raw is None:
            return
        try:
            text = os.fsdecode(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if text.startswith("file:"):                       # sqlite3 URI
            text = text[5:].split("?", 1)[0]
        if text in ("", ":memory:"):
            return
        path = real(os.path.abspath(os.path.expanduser(text)))
        ok_dir = under(path, allowed)
        if under(path, libs) and not ok_dir:
            # Работна папка в /usr/src/app (Docker, системен Python) е позволена.
            if write:   # `.pth` в user site тръгва преди hook-а следващия път
                raise PermissionError(f"[SANDBOX] запис в библиотеките на Python отказан: {text}")
            return
        if secret(text) or secret(path) or (under(path, blocked) and not ok_dir):
            raise PermissionError(f"[SANDBOX] достъп до чувствителен път отказан: {text}")

    def is_write(event: str, args: tuple) -> bool:
        if event in _EVENTS_WRITE:
            return True
        if event == "open" and len(args) >= 3:
            mode, flags = args[1], args[2]
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                return True
            return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
        return False

    busy = False

    def hook(event: str, args: tuple) -> None:
        nonlocal busy
        if busy:
            return
        if event in _EVENTS_PROC:
            parts: list[object] = []
            for a in args[:2]:
                parts.extend(a if isinstance(a, (list, tuple)) else [a])
            words = [os.fsdecode(x) for x in parts if isinstance(x, (str, bytes, os.PathLike))]
            line = " ".join(words)
            # Детето е без hook: поне командният ред не сочи тайна.
            if secret(line) or any(b and b in line for b in blocked):
                raise PermissionError("[SANDBOX] команда към чувствителен път отказана")
            if procs or _system_tool(words):
                # Само Popen стига до fork_exec/CreateProcess; ако Python избере
                # posix_spawn, отметката се сваля — иначе следващото пускане
                # от multiprocessing минаваше веднъж.
                _cleared.ok = event == "subprocess.Popen"
                return
            raise PermissionError("[SANDBOX] пускане на процес без одобрение — отказано")
        if event not in _EVENTS_READ and event not in _EVENTS_WRITE:
            return
        busy = True
        try:
            write = is_write(event, args)
            targets = args[:1] if event not in ("shutil.copyfile", "shutil.copytree",
                                                "os.rename", "os.replace", "os.symlink",
                                                "os.link") else args[:2]
            for i, raw in enumerate(targets):
                if isinstance(raw, (str, bytes, os.PathLike, int)) or raw is None:
                    # copyfile/copytree: изходът се чете, само целта е запис —
                    # копие от /usr/share се смяташе за запис в /usr (преглед).
                    copy = event in ("shutil.copyfile", "shutil.copytree")
                    check(raw, (write or copy and i == 1) and not (copy and i == 0), event)
        finally:
            busy = False

    sys.addaudithook(hook)
    global _check
    _check = check


# Само за четене на сведения за системата: stdlib ги пуска сама (platform.
# processor → uname, ctypes.util.find_library → ldconfig/gcc). Без тях тихо
# връщаше '' и None (преглед 2026-10-09). Аргументите пак се проверяват горе.
_SYSTEM_TOOLS = {"uname", "ldconfig", "gcc", "cc", "ld", "objdump", "sw_vers", "sysctl",
                 "lsb_release", "getconf", "nproc"}


def _system_tool(words: list[str]) -> bool:
    if not words:
        return False
    first = os.path.basename(words[0]).lower()
    if first in ("sh", "/bin/sh", "bash") and len(words) >= 3 and words[1] == "-c":
        first = os.path.basename(words[2].split()[0]).lower() if words[2].split() else ""
    return first.removesuffix(".exe") in _SYSTEM_TOOLS


_check: Callable[[object, bool, str], None] | None = None
_cleared = threading.local()


def _no_dir_fd() -> None:
    """os.open(име, dir_fd=…): името е спрямо чужда папка, а събитието „open“
    не носи dir_fd — относителното име се вижда като спрямо cwd (одит). Папката
    на dir_fd се намира през /proc и се проверява целият път (така fwalk и
    rmtree работят); без /proc — отказ."""
    real_open = os.open

    def guarded_open(path, flags, mode=0o777, *, dir_fd=None):  # type: ignore[no-untyped-def]
        if dir_fd is not None:
            try:
                base = os.readlink(f"/proc/self/fd/{dir_fd}")
            except OSError:
                raise PermissionError("[SANDBOX] os.open с dir_fd е изключен") from None
            if _check is not None:
                write = bool(flags & _WRITE_FLAGS)
                _check(os.path.join(base, os.fsdecode(path)), write, "")
            return real_open(path, flags, mode, dir_fd=dir_fd)
        return real_open(path, flags, mode)
    os.open = guarded_open  # type: ignore[assignment]
    backend = sys.modules.get("posix") or sys.modules.get("nt")
    if backend is not None:
        backend.open = guarded_open  # type: ignore[attr-defined]
    if "shutil" in sys.modules and not os.path.isdir("/proc/self/fd"):
        sys.modules["shutil"]._use_fd_functions = False  # type: ignore[attr-defined]


def _no_raw_spawn() -> None:
    """multiprocessing („spawn“) и всичко, което стига до създаването на
    процес покрай subprocess.Popen — без събитие за hook-а."""
    for name, attr in (("_posixsubprocess", "fork_exec"), ("_winapi", "CreateProcess")):
        try:
            mod = __import__(name)
            real = getattr(mod, attr)
        except (ImportError, AttributeError):   # няма го на тази платформа
            continue

        def gate(*a: object, _real: object = real, **k: object) -> object:
            # Само Popen, който hook-ът е пропуснал (системен инструмент) —
            # multiprocessing стига тук без събитие и остава отказан.
            if not getattr(_cleared, "ok", False):
                raise PermissionError("[SANDBOX] пускане на процес без одобрение — отказано")
            _cleared.ok = False
            return _real(*a, **k)  # type: ignore[operator]
        setattr(mod, attr, gate)


def main() -> None:
    if len(sys.argv) < 2:
        print("употреба: py_guard.py <script.py>", file=sys.stderr)
        sys.exit(2)
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    sys.path[0] = os.path.dirname(os.path.abspath(script))
    _install()
    _no_dir_fd()
    if os.environ.get("GENESIS_GUARD_PROCS") != "1":
        _no_raw_spawn()
    import runpy
    try:
        runpy.run_path(script, run_name="__main__")
    except SystemExit:
        raise
    except BaseException as e:
        # Следата — от кадъра на скрипта нататък, както без пазача: викащите
        # режат stderr отначало и „boom“ се губеше зад кадрите на runpy.
        import traceback
        target = os.path.abspath(script)
        tb = e.__traceback__
        while tb is not None and os.path.abspath(tb.tb_frame.f_code.co_filename) != target:
            tb = tb.tb_next
        traceback.print_exception(type(e), e, tb)
        sys.exit(1)


if __name__ == "__main__":
    main()
