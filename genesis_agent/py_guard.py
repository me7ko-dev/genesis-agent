"""
genesis_agent.py_guard — стартов скрипт за Python код от модела (2026-10-09).

    python py_guard.py <script.py>

Слага `sys.addaudithook`, който отказва всяко отваряне, листване или пускане
на процес към тайните — по ИСТИНСКИЯ път, без значение как е сглобен — и
пуска скрипта с runpy. Защо: проверката в sandbox.assess_code вижда само
буквални пътища в кода. `Path.home().parents[1] / ".ssh"`, `pwd.getpwuid()`,
`"~/.s" + "sh"` минаваха (одит 2026-10-09, възпроизведено). Hook-ът не може да
бъде махнат от кода след това (sys.addaudithook е за целия процес).

Не е стена: ctypes вика libc направо, без събития. Истинската граница е
отделен потребител/контейнер. Но всеки обикновен път през Python е затворен.

Средата (от sandbox.run_python):
  GENESIS_GUARD_RX     — регексът на тайните (sandbox._SECRET_PATHS)
  GENESIS_GUARD_BLOCK  — папки изцяло забранени (~/.genesis), с os.pathsep
  GENESIS_GUARD_ALLOW  — папки, които винаги може (скриптът, работната папка)
"""
from __future__ import annotations

import os
import re
import runpy
import sys

_EVENTS_PATH = {"open", "os.listdir", "os.scandir", "os.chdir", "os.remove", "os.rename",
                "os.replace", "os.rmdir", "shutil.copyfile", "shutil.copytree",
                "shutil.rmtree", "os.symlink", "os.link", "os.truncate", "os.utime",
                "os.chmod", "os.chown"}
_EVENTS_CMD = {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn"}


def _install() -> None:
    rx = re.compile(os.environ.get("GENESIS_GUARD_RX") or r"(?!x)x", re.IGNORECASE)
    split = os.pathsep

    def real(p: str) -> str:
        try:
            return os.path.realpath(p)
        except (OSError, ValueError):
            return p

    blocked = [real(d) for d in (os.environ.get("GENESIS_GUARD_BLOCK") or "").split(split) if d]
    allowed = [real(d) for d in (os.environ.get("GENESIS_GUARD_ALLOW") or "").split(split) if d]
    # Библиотеките на Python се четат свободно: иначе `google/auth/credentials.py`
    # би бил „тайна“ и внасянето му — отказано.
    import site
    libs = {real(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    try:
        libs.update(real(p) for p in site.getsitepackages())
        libs.add(real(site.getusersitepackages()))
    except Exception:
        pass
    busy = False

    def under(path: str, roots: list[str] | set[str]) -> bool:
        return any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) for r in roots)

    def check(raw: object) -> None:
        if isinstance(raw, int) or raw is None:
            return
        try:
            text = os.fsdecode(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        path = real(os.path.abspath(os.path.expanduser(text)))
        if under(path, allowed) or under(path, libs):
            return
        if rx.search(text) or rx.search(path) or under(path, blocked):
            raise PermissionError(f"[SANDBOX] достъп до чувствителен път отказан: {text}")

    def hook(event: str, args: tuple) -> None:
        nonlocal busy
        if busy:
            return
        if event not in _EVENTS_PATH and event not in _EVENTS_CMD:
            return
        busy = True
        try:
            if event in _EVENTS_PATH:
                for raw in args[:2]:
                    if isinstance(raw, (str, bytes, os.PathLike)):
                        check(raw)
            else:
                parts: list[object] = []
                for a in args[:2]:
                    if isinstance(a, (list, tuple)):
                        parts.extend(a)
                    else:
                        parts.append(a)
                line = " ".join(os.fsdecode(p) for p in parts
                                if isinstance(p, (str, bytes, os.PathLike)))
                if rx.search(line) or any(b and b in line for b in blocked):
                    raise PermissionError("[SANDBOX] команда към чувствителен път отказана")
        finally:
            busy = False

    sys.addaudithook(hook)


def main() -> None:
    if len(sys.argv) < 2:
        print("употреба: py_guard.py <script.py>", file=sys.stderr)
        sys.exit(2)
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    sys.path[0] = os.path.dirname(os.path.abspath(script))
    _install()
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
