"""Сървърът: всяка задача в отделен, изхвърлим контейнер с таван на всичко.

    python -m cloud.runner.launch --workspace /srv/jobs/42 "направи ми скрипт за ..."

Какво получава контейнерът и нищо повече:
  - папката на задачата като /work (единственото място за запис);
  - /tmp и домашна папка в паметта (tmpfs), всичко друго е само за четене;
  - 1 CPU, 1 GB RAM без swap, 256 процеса, 10 минути (по подразбиране);
  - без Linux capabilities, без повишаване на правата, потребител 10001;
  - мрежа само през egress проксито (cloud/egress/proxy.py) → само API на модели,
    или (режим с шлюз) само до шлюза — в СВОЯ вътрешна мрежа `genesis-job-…`,
    която живее колкото задачата: другите задачи не се виждат.

Изходът са JSON редовете от cloud/runner/task.py; последният е „done" с
токените и секундите — по тях се таксува.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cloud.gateway import gateway as gw

IMAGE = os.environ.get("GENESIS_RUNNER_IMAGE", "genesis-runner:latest")
GATEWAY_URL = os.environ.get("GENESIS_GATEWAY_URL", "http://genesis-gateway:8090")
TASK_BUDGET = int(os.environ.get("GENESIS_TASK_BUDGET", "300000"))  # токена на задача
JOBS_NETWORK = os.environ.get("GENESIS_JOBS_NETWORK", "genesis-jobs")
EGRESS_URL = os.environ.get("GENESIS_EGRESS_URL", "http://genesis-egress:3128")
RUNNER_UID = 10001
# Деплой одит 2026-10-11: всички задачи бяха в една обща genesis-jobs (ICC
# включен) — задача на човек А прочете /work на човек Б през `http.server` по
# име и по IP. Сега всяка задача е в своя --internal мрежа, в която е само
# помощникът ѝ (шлюзът ИЛИ проксито). Измерено тук: другата задача → „Network is
# unreachable“, името ѝ не се резолвира, шлюзът отговаря 200; цената е ~1.2 s
# на задача (create + connect + disconnect + rm, Docker 29.8.2).
TASK_PREFIX = "genesis-task-"
NETWORK_PREFIX = "genesis-job-"
LABEL = "genesis.task=1"


@dataclass(frozen=True)
class Limits:
    cpus: str = "1"
    memory: str = "1g"
    pids: int = 256
    seconds: int = 600
    tmp_mb: int = 256


@dataclass(frozen=True)
class Gateway:
    """Режим с шлюз (cloud/gateway): ключовете не влизат в контейнера.

    Контейнерът получава жетон за задачата вместо всеки ключ, който шлюзът
    има, и адреса на шлюза; изход навън няма (нито прокси). Токените за
    сметката се четат от `usage` на шлюза, не от събитията на контейнера.
    """
    secret: str
    usage: Path
    key_envs: tuple[str, ...]
    url: str = GATEWAY_URL
    budget: int = TASK_BUDGET

    @classmethod
    def from_keys_file(cls, keys: Path, usage: Path, **kw: Any) -> Gateway:
        k = gw.read_keys(keys)
        secret = k.pop("GATEWAY_SECRET", "")
        if len(secret) < 32:
            raise ValueError(f"{keys}: липсва GATEWAY_SECRET (поне 32 знака)")
        envs = tuple(gw.UPSTREAMS[p][1] for p in gw.served_providers(k))
        return cls(secret, usage, envs, **kw)


@dataclass
class Result:
    ok: bool
    timed_out: bool
    exit_code: int
    seconds: float
    tokens: dict[str, int] = field(default_factory=dict)
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    # Името на контейнера на задачата: cloud/check.py проверява, че махнатото е
    # точно нейното, не и задачите на клиенти на живия сървър (ревю 2026-10-11).
    container: str = ""

# Най-дългият ред събитие от контейнера, който се чете (1 MiB).
_MAX_EVENT_LINE = 1 << 20
# Таван на ход: брой събития и общ размер (одит 2026-10-11). Само таванът на
# реда пускаше 300 събития по 1 MiB — 300 MiB web.db и 626 MiB RSS на уеб
# сървъра от един ход; кодът в контейнера пише в същия stdout като агента.
_MAX_EVENTS = 2000
_MAX_EVENT_BYTES = 8 << 20
# Ревю 2026-10-11: ред `{"kind":"assistant","text":"\\udcee"}` от кода в
# контейнера става lone surrogate след json.loads; store.add_event (json.dumps
# в sqlite) гърмеше с UnicodeEncodeError, ходът падаше и `docker run`
# оставаше да върви без таван. Escape-ът се търси в суровия ред — така
# обикновените събития не се кодират втори път.
_SURROGATE_ESCAPE = re.compile(rb"\\u[dD][89a-fA-F]")
_SURROGATE = re.compile("[\ud800-\udfff]")


def _parse_event(raw: bytes) -> dict[str, Any] | None:
    """Събитие от реда на контейнера, или None за шум. Без lone surrogates:
    всяко събитие оттук се записва и праща като UTF-8."""
    try:
        ev = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(ev, dict) or "kind" not in ev:
            return None
        if _SURROGATE_ESCAPE.search(raw):
            # Двойките вече са слети от json.loads — всеки останал surrogate е
            # самотен и е само в низ, затова замяната оставя валиден JSON.
            ev = json.loads(_SURROGATE.sub("\ufffd", json.dumps(ev, ensure_ascii=False)))
    except (ValueError, RecursionError):
        # RecursionError: ред `[` * 200000 от кода в контейнера (ревю 2026-10-11).
        return None  # шум от Python/rich — не е събитие
    return ev


def _docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Една `docker` команда без shell. Грешката е в returncode/stderr, не
    изключение — извикващият решава дали тя спира задачата. Тестовете я
    подменят (tests/conftest.py): нито един тест не пипа истинския Docker."""
    argv = ["docker", *args]
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(e).__name__}: {e}")


def helper_container(url: str) -> str:
    """Името на контейнера зад адреса (http://genesis-gateway:8090 →
    genesis-gateway): по това име той се включва в мрежата на задачата."""
    return urlsplit(url).hostname or ""


def open_task_network(network: str, helper: str) -> str:
    """Създава вътрешната мрежа на задачата и включва помощника в нея.
    Връща грешката ("" при успех) — без мрежа задачата не тръгва."""
    r = _docker("network", "create", "--internal", "--label", LABEL, network)
    if r.returncode:
        return f"мрежата за задачата не се създаде: {r.stderr.strip()[-300:]}"
    r = _docker("network", "connect", network, helper)
    if r.returncode:
        _docker("network", "rm", network)
        return (f"{helper} не може да се включи в мрежата на задачата (пуснат ли е "
                f"./cloud/up.sh?): {r.stderr.strip()[-300:]}")
    return ""


def close_task_network(network: str, name: str, helper: str) -> None:
    """Махa контейнера (ако още е там), откача помощника и трие мрежата.
    Грешките се пренебрегват: каквото остане, го чисти cleanup_leftovers."""
    _docker("rm", "-f", name)
    _docker("network", "disconnect", "-f", network, helper)
    _docker("network", "rm", network)


def cleanup_leftovers() -> int:
    """Убива всички контейнери на задачи и трие мрежите им. Пуска се при старт
    и спиране на уеб сървъра (и от systemd — cloud/deploy/genesis-web.service).

    Деплой одит 2026-10-11: единственият таван беше таймерът в уеб процеса. При
    SIGTERM/SIGKILL на сървъра контейнерът оставаше Up без срок (1 CPU, 1 GB), а
    след рестарт ходът ставаше „failed“ и човекът пускаше втори контейнер в
    същата папка. Връща колко контейнера е спрял."""
    r = _docker("ps", "-aq", "--filter", f"name=^{TASK_PREFIX}")
    ids = r.stdout.split() if r.returncode == 0 else []
    if ids:
        _docker("rm", "-f", *ids)
    r = _docker("network", "ls", "--format", "{{.Name}}", "--filter", f"name={NETWORK_PREFIX}")
    # Филтърът на Docker е подниз — `genesis-jobs` (ръчните проверки) не се пипа.
    for net in (n for n in r.stdout.split() if r.returncode == 0 and n.startswith(NETWORK_PREFIX)):
        attached = _docker("network", "inspect", "-f",
                           "{{range .Containers}}{{.Name}} {{end}}", net)
        for c in attached.stdout.split() if attached.returncode == 0 else []:
            _docker("network", "disconnect", "-f", net, c)
        _docker("network", "rm", net)
    return len(ids)


def _stop(proc: subprocess.Popen[bytes], name: str) -> None:
    """Спира контейнера и клиента `docker run` и ги изчаква. Само убиването
    на клиента не стига: контейнерът продължава без него."""
    _docker("kill", name, timeout=30)
    try:
        proc.kill()
        proc.wait(timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def docker_args(name: str, workspace: Path, text: str, *, limits: Limits | None = None,
                image: str = IMAGE, network: str = JOBS_NETWORK,
                egress_url: str = EGRESS_URL, env_file: str | None = None,
                runtime: str | None = None, gateway: Gateway | None = None,
                token: str = "") -> list[str]:
    """Целият `docker run` за една задача. Отделна функция, за да се тества
    без Docker: всяко ограничение тук е обещание към клиента."""
    limits = limits or Limits()
    args = [
        "docker", "run", "--rm", "--name", name,
        "--label", LABEL,
        # --init: PID 1 е docker-init, а не python. Python като PID 1 пренебрегва
        # SIGTERM, който `docker run` препраща, когато systemd спира уеб сървъра
        # (деплой одит 2026-10-11) — с --init сигналът спира задачата.
        "--init",
        "--network", network,
        "--cpus", limits.cpus,
        "--memory", limits.memory, "--memory-swap", limits.memory,
        "--pids-limit", str(limits.pids),
        "--read-only",
        "--tmpfs", f"/tmp:rw,nosuid,nodev,size={limits.tmp_mb}m",
        "--tmpfs", f"/home/agent:rw,nosuid,nodev,size=64m,uid={RUNNER_UID},gid={RUNNER_UID}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--user", f"{RUNNER_UID}:{RUNNER_UID}",
        "-v", f"{workspace.resolve()}:/work:rw",
        "-e", "HOME=/home/agent",
        "-e", "GENESIS_WORKSPACE=/work",
        "-e", "PYTHONIOENCODING=utf-8",
        # Втори таван вътре в контейнера (task.py): ако уеб процесът умре и
        # никой не убие контейнера, задачата пак свършва (+30 s след своя).
        "-e", f"GENESIS_TASK_DEADLINE={limits.seconds + 30}",
    ]
    if gateway is not None:
        # Без прокси и без ключове: единственият адрес е шлюзът.
        args += ["-e", f"GENESIS_MODEL_GATEWAY={gateway.url}"]
        for env in gateway.key_envs:
            args += ["-e", f"{env}={token}"]
    else:
        args += [
            "-e", f"HTTPS_PROXY={egress_url}", "-e", f"HTTP_PROXY={egress_url}",
            "-e", f"https_proxy={egress_url}", "-e", f"http_proxy={egress_url}",
            # localhost направо: в контейнера няма ollama, пробата трябва да падне
            # веднага, не да минава през проксито.
            "-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1",
        ]
        if env_file:
            args += ["--env-file", env_file]
    if runtime:  # напр. "runsc" (gVisor) — препоръчително на истинския сървър
        args += ["--runtime", runtime]
    return [*args, image, text]


def prepare_workspace(workspace: Path) -> None:
    """Папката на задачата трябва да е записваема от потребител 10001."""
    workspace.mkdir(parents=True, exist_ok=True)
    chown = getattr(os, "chown", None)  # няма го на Windows (там се тества)
    try:
        if chown is None:
            raise OSError("no chown")
        chown(workspace, RUNNER_UID, RUNNER_UID)
    except OSError:
        workspace.chmod(0o777)


def run_task(text: str, workspace: Path, *, limits: Limits | None = None,
             on_event: Callable[[dict[str, Any]], None] | None = None,
             env_file: str | None = None, runtime: str | None = None,
             image: str = IMAGE, gateway: Gateway | None = None,
             egress_url: str = EGRESS_URL) -> Result:
    limits = limits or Limits()
    suffix = uuid.uuid4().hex[:12]
    name = f"{TASK_PREFIX}{suffix}"
    network = f"{NETWORK_PREFIX}{suffix}"
    helper = helper_container(gateway.url if gateway is not None else egress_url)
    prepare_workspace(workspace)
    error = open_task_network(network, helper)
    if error:
        return Result(False, False, -1, 0.0, error=error, container=name)
    try:
        res = _run_in(network, name, text, workspace, limits=limits, on_event=on_event,
                      env_file=env_file, runtime=runtime, image=image, gateway=gateway,
                      egress_url=egress_url)
    finally:
        close_task_network(network, name, helper)
    res.container = name
    return res


def _run_in(network: str, name: str, text: str, workspace: Path, *, limits: Limits,
            on_event: Callable[[dict[str, Any]], None] | None, env_file: str | None,
            runtime: str | None, image: str, gateway: Gateway | None,
            egress_url: str) -> Result:
    token = ""
    usage_start = 0
    if gateway is not None:
        usage_start = gw.usage_size(gateway.usage)
        # Жетонът живее колкото задачата (+1 мин за последния отговор).
        token = gw.make_token(gateway.secret, name, budget=gateway.budget,
                              ttl=limits.seconds + 60)
    argv = docker_args(name, workspace, text, limits=limits, env_file=env_file,
                       runtime=runtime, image=image, gateway=gateway, token=token,
                       network=network, egress_url=egress_url)
    t0 = time.monotonic()
    timed_out = threading.Event()

    def _watchdog() -> None:
        timed_out.set()
        _docker("kill", name)

    timer = threading.Timer(limits.seconds, _watchdog)
    timer.start()
    events: list[dict[str, Any]] = []
    # stderr отива във временен файл, не в тръба: контейнер, който пише над
    # 64 KB предупреждения, блокираше на пълната тръба, stdout никога не
    # стигаше до края и завършен ход се убиваше на тавана (одит 2026-10-07).
    err_file = tempfile.TemporaryFile()  # noqa: SIM115 — затваря се във finally, ползва се от Popen
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err_file)
        assert proc.stdout is not None
        skipping = False
        kept_bytes = 0
        capped = False
        late_done: dict[str, Any] | None = None
        while True:
            # Ред с таван: един ред без край от контейнера иначе изяждаше
            # паметта на уеб сървъра (400 MiB → 826 MiB RSS).
            raw = proc.stdout.readline(_MAX_EVENT_LINE)
            if not raw:
                break
            complete = raw.endswith(b"\n")
            if skipping or not complete and len(raw) >= _MAX_EVENT_LINE:
                skipping = not complete
                continue
            ev = _parse_event(raw)
            if ev is None:
                continue
            if not capped and (len(events) >= _MAX_EVENTS
                               or kept_bytes + len(raw) > _MAX_EVENT_BYTES):
                capped = True
                warn = {"kind": "warn", "text": "изходът е отрязан — твърде много събития в един ход"}
                events.append(warn)
                if on_event:
                    on_event(warn)
            if capped:
                # Над тавана само последното „done“ — с него е сметката.
                if ev.get("kind") == "done":
                    late_done = ev
                continue
            kept_bytes += len(raw)
            events.append(ev)
            if on_event:
                on_event(ev)
        if late_done is not None:
            events.append(late_done)
            if on_event:
                on_event(late_done)
        code = proc.wait()
        err_file.seek(0, os.SEEK_END)
        err_file.seek(max(0, err_file.tell() - 8192))
        stderr = err_file.read().decode("utf-8", "replace")
    except BaseException:
        # Ревю 2026-10-11: всяка грешка тук (on_event, sqlite, Ctrl+C, нов вид
        # лош ред) излизаше от run_task, finally спираше само наблюдателя и
        # `docker run` оставаше да върви без таван. Причината не се изброява
        # по тип — каквото и да е, контейнерът се убива и изчаква.
        if proc is not None:
            _stop(proc, name)
        raise
    finally:
        timer.cancel()
        err_file.close()

    done = next((e for e in reversed(events) if e.get("kind") == "done"), None)
    seconds = round(time.monotonic() - t0, 1)
    # С шлюз сметката е от шлюза — контейнерът може да каже каквото си иска.
    billed = gw.usage_for(gateway.usage, name, usage_start) if gateway is not None else None
    if timed_out.is_set():
        return Result(False, True, code, seconds, error=f"прекъснато след {limits.seconds} s",
                      tokens=billed or {}, events=events)
    if done is None:
        return Result(False, False, code, seconds,
                      error=(stderr.strip().splitlines() or ["контейнерът спря без резултат"])[-1][:500],
                      tokens=billed or {}, events=events)
    return Result(bool(done.get("ok")) and code == 0, False, code, seconds,
                  tokens=billed if billed is not None else dict(done.get("tokens") or {}),
                  error=str(done.get("error") or ""), events=events)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Една задача в затворен контейнер.")
    p.add_argument("text")
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument("--seconds", type=int, default=Limits.seconds)
    p.add_argument("--env-file", help="API ключовете за моделите (KEY=value на ред)")
    p.add_argument("--runtime", help="напр. runsc (gVisor)")
    p.add_argument("--gateway-usage", type=Path,
                   help="режим с шлюз: JSONL на шлюза; --env-file държи GATEWAY_SECRET")
    a = p.parse_args(argv)
    gateway = (Gateway.from_keys_file(Path(a.env_file), a.gateway_usage)
               if a.gateway_usage and a.env_file else None)
    res = run_task(a.text, a.workspace, limits=Limits(seconds=a.seconds),
                   on_event=lambda e: print(json.dumps(e, ensure_ascii=False), flush=True),
                   env_file=a.env_file, runtime=a.runtime, gateway=gateway)
    print(json.dumps({"kind": "result", "ok": res.ok, "timed_out": res.timed_out,
                      "exit_code": res.exit_code, "seconds": res.seconds,
                      "tokens": res.tokens, "error": res.error}, ensure_ascii=False))
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
