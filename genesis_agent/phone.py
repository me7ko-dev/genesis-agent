"""genesis_agent.phone — `genesis phone`: Genesis на самия телефон, без компютър.

Android няма python, git и node. Termux ги дава: истински Linux на телефона,
в който агентът тече точно както на компютъра. Приложението Genesis
(mobile/, преди „Genesis Remote“) е прозорецът към него. Устройството е като при Genesis Desktop:
`genesis serve` слуша само на 127.0.0.1, а приложението говори с него по
криптирания протокол от remote_server.py.

    genesis phone start    пуска агента във фонов режим (ако вече не тече)
    genesis phone stop     спира го
    genesis phone status   тече ли и къде работи
    genesis phone pair     отваря приложението Genesis, сдвоено с агента тук
    genesis phone log      последните редове от изхода му

`start` се вика от бутона „Пусни Genesis" в приложението (Termux RUN_COMMAND),
от Termux:Boot при включване на телефона и на ръка. Инсталацията е в
scripts/install-termux.sh.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from genesis_agent.paths import GENESIS_HOME
from genesis_agent.remote_server import DEFAULT_PORT

HOST = "127.0.0.1"
APP_SCHEME = "genesisremote"
_START_WAIT_S = 90.0     # на телефон първото пускане (системният промпт, паметта) е бавно
_STOP_WAIT_S = 15.0
_LOG_MAX = 1024 * 1024

USAGE = """Употреба: genesis phone <команда> [--port N]

Genesis на този телефон (Android, Termux) — без компютър.

  start    пуска агента във фонов режим (ако вече не тече)
  stop     спира го
  status   тече ли и къде работи
  pair     отваря приложението Genesis, сдвоено с агента тук
  log      последните редове от изхода му

Инсталация: scripts/install-termux.sh (виж docs/ANDROID.md)."""


def state_dir() -> Path:
    return GENESIS_HOME / "phone"


def pid_file() -> Path:
    return state_dir() / "serve.pid"


def log_file() -> Path:
    return state_dir() / "serve.log"


def workspace() -> Path:
    """Където агентът работи на телефона: ~/genesis, освен ако не е зададено друго.

    В Termux, не в споделената памет (~/storage/shared): там няма права за
    изпълнение и символни връзки, тоест git, npm и `./скрипт` не биха работили."""
    env = os.environ.get("GENESIS_WORKSPACE")
    return Path(env).expanduser() if env else Path.home() / "genesis"


def hello(port: int = DEFAULT_PORT, timeout: float = 2.0) -> dict | None:
    """Отговорът на /api/hello, ако тук тече Genesis; иначе None."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/hello", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data if isinstance(data, dict) and data.get("app") == "genesis" else None


def app_link(pairing: str) -> str:
    """Връзката, която отваря приложението направо сдвоено: genesisremote://pair?u=…

    Приложението (mobile/src/app/pair.tsx) чете `u` и сдвоява с него, както
    със сканиран QR код. Ключът не напуска телефона: Android подава връзката
    от Termux директно на приложението."""
    return f"{APP_SCHEME}://pair?u={quote(pairing, safe='')}"


def _serve_argv(port: int) -> list[str]:
    return [sys.executable, "-m", "genesis_agent.cli", "serve", "--bind", HOST, "--port", str(port)]


def _read_pid() -> int | None:
    try:
        return int(pid_file().read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_ours(pid: int) -> bool:
    """Процесът с този pid още ли е нашият `genesis serve`? pid-ът от файла може
    да е преизползван от нещо съвсем друго след рестарт на телефона."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    args = [a.decode("utf-8", "replace") for a in cmdline if a]
    return "serve" in args and any("genesis" in a for a in args)


def _termux(tool: str, *args: str) -> bool:
    """termux-wake-lock, termux-open-url… — само ако ги има (извън Termux ги няма)."""
    exe = shutil.which(tool)
    if not exe:
        return False
    try:
        r = subprocess.run([exe, *args], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _open_log():
    path = log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > _LOG_MAX:
            path.replace(path.with_suffix(".log.1"))
    except OSError:
        pass
    return open(path, "ab")  # детето го наследява; start() затваря своето копие веднага


def tail(lines: int = 40) -> str:
    try:
        text = log_file().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def start(port: int = DEFAULT_PORT, wait: float = _START_WAIT_S) -> int:
    if hello(port):
        print(f"Genesis вече тече на този телефон (порт {port}).")
        return 0
    if os.name == "nt":
        print("`genesis phone` е за Android (Termux). На компютъра: Genesis Desktop или `genesis serve`.")
        return 2
    ws = workspace()
    ws.mkdir(parents=True, exist_ok=True)
    # Без wake lock Android приспива Termux при изгасен екран и агентът
    # замръзва насред работа. Termux показва известие, докато го държи.
    _termux("termux-wake-lock")

    env = dict(os.environ, GENESIS_WORKSPACE=str(ws), PYTHONUNBUFFERED="1")
    log = _open_log()
    try:
        proc = subprocess.Popen(_serve_argv(port), cwd=str(ws), env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        print(f"Не мога да пусна Genesis: {e}")
        return 1
    finally:
        log.close()
    pid_file().write_text(str(proc.pid), encoding="utf-8")

    print(f"Пускам Genesis (работна папка {ws})…", flush=True)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pid_file().unlink(missing_ok=True)
            print(f"Genesis спря веднага (код {proc.returncode}). Последното от изхода му:\n")
            print(tail(25))
            return 1
        if hello(port, timeout=1.0):
            print(f"Genesis тече на този телефон (порт {port}). Отвори приложението Genesis.")
            return 0
        time.sleep(0.5)
    print("Genesis още тръгва — провери след малко с `genesis phone status` "
          "(изходът: `genesis phone log`).")
    return 0


def stop(port: int = DEFAULT_PORT) -> int:
    pid = _read_pid()
    if pid is None or not _alive(pid) or not _is_ours(pid):
        pid_file().unlink(missing_ok=True)
        if hello(port):
            print("Genesis тече, но не е пуснат с `genesis phone start` — спри го с Ctrl+C "
                  "в сесията на Termux, в която е пуснат.")
            return 1
        print("Genesis не тече.")
        _termux("termux-wake-unlock")
        return 0
    # SIGINT, не SIGTERM: serve() го хваща като Ctrl+C и прибира паметта от
    # разговора, преди да излезе. Упоритият процес получава SIGKILL.
    for sig, grace in ((signal.SIGINT, _STOP_WAIT_S), (signal.SIGKILL, 5.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and _alive(pid) and _is_ours(pid):
            time.sleep(0.2)
        if not (_alive(pid) and _is_ours(pid)):
            break
    pid_file().unlink(missing_ok=True)
    _termux("termux-wake-unlock")
    print("Genesis е спрян.")
    return 0


def status(port: int = DEFAULT_PORT) -> int:
    info = hello(port)
    if not info:
        print("Genesis не тече. Пусни го: genesis phone start")
        return 1
    pid = _read_pid()
    print(f"Genesis тече на този телефон: {info.get('name', '')}, порт {port}"
          + (f", pid {pid}" if pid and _alive(pid) else ""))
    print(f"Работна папка: {workspace()}")
    print(f"Изход: {log_file()}")
    return 0


def pair(port: int = DEFAULT_PORT) -> int:
    """Пуска агента, ако трябва, и отваря приложението с връзката за сдвояване."""
    if not hello(port):
        code = start(port)
        if code or not hello(port):
            return code or 1
    from genesis_agent import remote_server as rs
    pairing = rs.pairing_url(HOST, port, rs.load_or_create_key(), rs.machine_name())
    link = app_link(pairing)
    if _termux("termux-open-url", link):
        print("Отварям приложението Genesis — то се сдвоява само.\n"
              "Ако не се отвори: инсталирай Genesis (genesis-remote-android.apk) и пусни пак "
              "`genesis phone pair`.")
        return 0
    print("Не можах да отворя приложението Genesis. Постави тази връзка в него "
          "(„…или постави връзката“):\n")
    print(pairing)
    return 1


def main(args: list[str]) -> int:
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if args else 2
    cmd, rest = args[0], args[1:]
    port = DEFAULT_PORT
    if rest[:1] == ["--port"] and len(rest) >= 2:
        try:
            port = int(rest[1])
        except ValueError:
            print(f"--port иска число, не {rest[1]!r}")
            return 2
        rest = rest[2:]
    if rest:
        print(f"Непозната опция: {rest[0]}\n\n{USAGE}")
        return 2
    if cmd == "start":
        return start(port)
    if cmd == "stop":
        return stop(port)
    if cmd == "status":
        return status(port)
    if cmd == "pair":
        return pair(port)
    if cmd == "log":
        print(tail() or f"Още няма изход ({log_file()}).")
        return 0
    print(f"Непозната команда: {cmd}\n\n{USAGE}")
    return 2
