"""genesis_agent.self_update — обновяване, без да пипа файла, докато тече.

`pipx install --force` подменя точно изпълнимия файл на инсталираното копие.
Докато Genesis тече, ТОЗИ файл е отворен от операционната система — на
Windows това означава заключен за запис (ERROR_SHARING_VIOLATION); на Linux
презаписът обикновено минава (старият inode остава да живее, докато старият
процес го държи), но „обикновено минава" не е обещание, а late-night сюрприз
за оператор, чакащ работеща команда.

Затова обновяването никога не тръгва от процеса, който трябва да бъде
подменен. `request_update()` пуска ТОЗИ модул като отделен, откачен процес
(subprocess.Popen(start_new_session=True)),
който:

  1. чака оригиналният PID да излезе напълно (`pid_alive`, полинг — POSIX
     през `os.kill(pid, 0)`, Windows през `OpenProcess`/`GetExitCodeProcess`,
     защото Windows изобщо няма POSIX сигнал 0 да провери с него);
  2. чак тогава намира истински работещ `pipx` (проверен чрез реално
     изпълнение, не по име на PATH — виж `find_pipx`, същият принцип като
     `Get-InterpreterVersion` в scripts/install_windows.ps1: sys.executable
     на ТОЗИ процес е интерпретаторът в pipx-венва на Genesis, който НЯМА
     pipx като модул — `sys.executable -m pipx` пада с ModuleNotFoundError
     въпреки че pipx съвсем реално го има на машината);
  3. записва резултата в GENESIS_HOME/update_state.json.

Следващото стартиране на `genesis` (`report_pending()`) го чете веднъж и го
трие — операторът вижда „обновено" или грешката, без да рови в лог файлове.

Родният Windows билд (genesis.exe, без Python и pipx) минава по същия път,
но откаченият процес е PowerShell с инсталатора scripts/install.ps1 (носен
в самия билд): той чака PID-а, сваля новия release, проверява SHA256 и
подменя папката на приложението цяла — `request_native_update`.

Проверено само по логика (subprocess е mock-нат в тестовете): истинско
заключване на .exe при запис, докато процесът тече, не може да се
симулира на тази (Linux) машина. Виж docs/WINDOWS.md.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from genesis_agent.paths import GENESIS_HOME

_STATE_FILE_NAME = "update_state.json"
_WAIT_POLL_SECONDS = 1.0
_WAIT_TIMEOUT_SECONDS = 120.0
_PIPX_TIMEOUT_SECONDS = 600
_PROBE_TIMEOUT_SECONDS = 10
_NATIVE_WAIT_TIMEOUT_SECONDS = 12 * 3600


def _state_path() -> Path:
    return GENESIS_HOME / _STATE_FILE_NAME


def _pid_alive_posix(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # съществува, просто не е наш процес
    except OSError:
        return True
    return True


def _pid_alive_win32(pid: int) -> bool:
    # Реална WinAPI — няма смислен mock на Linux без да тества самия ctypes,
    # не логиката. Проверено на живо само от windows-latest CI leg-а (виж
    # tests/test_self_update.py: skipif извън win32). Дотук нищо друго в
    # този модул не пипа ctypes.windll.
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
            handle, ctypes.byref(exit_code))
        return bool(ok) and exit_code.value == STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]


def pid_alive(pid: int) -> bool:
    """Все още ли тече процесът с този PID. При всяко съмнение — приема, че да,
    за да не тръгне pipx твърде рано; никога не хвърля."""
    if sys.platform == "win32":
        return _pid_alive_win32(pid)
    return _pid_alive_posix(pid)


def find_pipx() -> list[str] | None:
    """Argv-префикс, който РЕАЛНО изпълнява pipx, или None.

    Никога не се доверява на едно-единствено предположение: `sys.executable`
    на този процес е интерпретаторът в GENESIS-ОВИЯ pipx венв, който няма
    pipx като модул. Изпробва всеки кандидат с истинско извикване, вместо да
    гадае по PATH или по име.
    """
    candidates: list[list[str]] = [["pipx"]]
    for py in ("py", "python3", "python"):
        candidates.append([py, "-m", "pipx"])
    for c in candidates:
        try:
            r = subprocess.run(c + ["--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=_PROBE_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return c
    return None


def _write_state(**fields: object) -> None:
    # Собствен mkdir, не paths.ensure_genesis_home(): онази чете GENESIS_HOME
    # от СВОЯ модул, не от името, монипатчнато тук в тестовете — резултатът
    # би бил тих запис в истинската ~/.genesis, докато тестът си мисли, че
    # пипа tmp_path.
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"ts": time.time(), **fields}
    path.write_text(json.dumps(payload), encoding="utf-8")


def run_updater(pid: int, url: str, ref: str) -> int:
    """Чака `pid` да излезе, после реално обновява. Извиква се само от
    отделния, откачен процес — никога директно от чат цикъла."""
    deadline = time.monotonic() + _WAIT_TIMEOUT_SECONDS
    while pid_alive(pid):
        if time.monotonic() > deadline:
            _write_state(ok=False,
                         error=f"процес {pid} не приключи за {_WAIT_TIMEOUT_SECONDS:.0f}s")
            return 1
        time.sleep(_WAIT_POLL_SECONDS)

    pipx = find_pipx()
    if pipx is None:
        _write_state(ok=False,
                     error="pipx не е намерен (нито `pipx`, нито `py/python3/python -m pipx`)")
        return 1

    spec = f"git+{url}" + (f"@{ref}" if ref else "")
    try:
        result = subprocess.run(pipx + ["install", "--force", spec],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=_PIPX_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        _write_state(ok=False, error=f"pipx install надхвърли {_PIPX_TIMEOUT_SECONDS}s")
        return 1
    if result.returncode != 0:
        _write_state(ok=False, error=(result.stderr or result.stdout or "").strip()[:500])
        return 1
    _write_state(ok=True, spec=spec)
    return 0


def native_update_argv(*, pid: int, ref: str, owner_repo: str,
                       script: Path, install_dir: Path) -> list[str]:
    """Командата за PowerShell обновяването на родния билд."""
    return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-File", str(script),
            "-WaitPid", str(pid), "-Tag", ref or "latest",
            "-Repo", owner_repo or "me7ko-dev/genesis-agent",
            "-InstallDir", str(install_dir), "-StateFile", str(_state_path()),
            # Чатът може да продължи часове след `/update`; pipx пътят
            # (_WAIT_TIMEOUT_SECONDS) се отказва след 2 минути.
            "-WaitTimeout", str(_NATIVE_WAIT_TIMEOUT_SECONDS),
            "-NoShortcut"]


def request_native_update(*, pid: int, url: str, ref: str) -> None:
    """Като `request_update`, за genesis.exe: инсталаторът се копира в %TEMP%
    и тече от там — копието в папката на приложението би я държало заето
    точно докато тя се подменя."""
    import shutil
    import tempfile

    from genesis_agent.paths import PROJECT_ROOT, install_dir
    from genesis_agent.version_info import Source

    target = install_dir()
    if target is None:
        raise RuntimeError("request_native_update извън родния билд")
    script = Path(tempfile.gettempdir()) / f"genesis-update-{pid}.ps1"
    shutil.copyfile(PROJECT_ROOT / "install.ps1", script)
    argv = native_update_argv(pid=pid, ref=ref, owner_repo=Source(url=url).owner_repo,
                              script=script, install_dir=target)
    # Скрита собствена конзола, не DETACHED_PROCESS: без конзола всяка
    # конзолна програма, която инсталаторът пуска (пробният genesis.exe
    # --version), би отворила свой видим прозорец.
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=no_window | new_group)


def request_update(*, pid: int, url: str, ref: str) -> None:
    """Пуска обновяването на заден план и се връща веднага — не чака нищо.

    `subprocess.DETACHED_PROCESS`/`CREATE_NEW_PROCESS_GROUP` съществуват
    само в модула, компилиран за Windows — четени са през `getattr` с
    резерва 0, за да могат тестове да упражнят тази клонка (с монипатчнат
    `sys.platform`) и на друга платформа, без AttributeError, без да
    променят реалното поведение на Windows.
    """
    from genesis_agent.paths import FROZEN
    if FROZEN:
        request_native_update(pid=pid, url=url, ref=ref)
        return
    argv = [sys.executable, "-m", "genesis_agent.self_update",
            "--pid", str(pid), "--url", url, "--ref", ref]
    if sys.platform == "win32":
        detached = getattr(subprocess, "DETACHED_PROCESS", 0)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=detached | new_group,
        )
    else:
        subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )


def report_pending() -> str | None:
    """Прочита и трие чакащ резултат от предишно `/update`, ако има."""
    path = _state_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        path.unlink(missing_ok=True)
        return None
    path.unlink(missing_ok=True)
    if data.get("ok"):
        return f"[green]✅ Обновено: {data.get('spec', '')}[/]"
    return f"[red]❌ Последното /update не мина: {data.get('error', 'неизвестна грешка')}[/]"


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Изпълнява се само от genesis_agent.self_update.request_update — "
                     "не се вика на ръка.")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--ref", default="")
    ns = parser.parse_args(argv)
    return run_updater(ns.pid, ns.url, ns.ref)


if __name__ == "__main__":
    raise SystemExit(_main())
