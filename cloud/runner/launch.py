"""Сървърът: всяка задача в отделен, изхвърлим контейнер с таван на всичко.

    python -m cloud.runner.launch --workspace /srv/jobs/42 "направи ми скрипт за ..."

Какво получава контейнерът и нищо повече:
  - папката на задачата като /work (единственото място за запис);
  - /tmp и домашна папка в паметта (tmpfs), всичко друго е само за четене;
  - 1 CPU, 1 GB RAM без swap, 256 процеса, 10 минути (по подразбиране);
  - без Linux capabilities, без повишаване на правата, потребител 10001;
  - мрежа само през egress проксито (cloud/egress/proxy.py) → само API на модели.

Изходът са JSON редовете от cloud/runner/task.py; последният е „done" с
токените и секундите — по тях се таксува.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IMAGE = os.environ.get("GENESIS_RUNNER_IMAGE", "genesis-runner:latest")
JOBS_NETWORK = os.environ.get("GENESIS_JOBS_NETWORK", "genesis-jobs")
EGRESS_URL = os.environ.get("GENESIS_EGRESS_URL", "http://genesis-egress:3128")
RUNNER_UID = 10001


@dataclass(frozen=True)
class Limits:
    cpus: str = "1"
    memory: str = "1g"
    pids: int = 256
    seconds: int = 600
    tmp_mb: int = 256


@dataclass
class Result:
    ok: bool
    timed_out: bool
    exit_code: int
    seconds: float
    tokens: dict[str, int] = field(default_factory=dict)
    error: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


def docker_args(name: str, workspace: Path, text: str, *, limits: Limits | None = None,
                image: str = IMAGE, network: str = JOBS_NETWORK,
                egress_url: str = EGRESS_URL, env_file: str | None = None,
                runtime: str | None = None) -> list[str]:
    """Целият `docker run` за една задача. Отделна функция, за да се тества
    без Docker: всяко ограничение тук е обещание към клиента."""
    limits = limits or Limits()
    args = [
        "docker", "run", "--rm", "--name", name,
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
        "-e", f"HTTPS_PROXY={egress_url}", "-e", f"HTTP_PROXY={egress_url}",
        "-e", f"https_proxy={egress_url}", "-e", f"http_proxy={egress_url}",
        # localhost направо: в контейнера няма ollama, пробата трябва да падне
        # веднага, не да минава през проксито.
        "-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1",
        "-e", "PYTHONIOENCODING=utf-8",
    ]
    if env_file:
        args += ["--env-file", env_file]
    if runtime:  # напр. "runsc" (gVisor) — препоръчително на истинския сървър
        args += ["--runtime", runtime]
    return [*args, image, text]


def prepare_workspace(workspace: Path) -> None:
    """Папката на задачата трябва да е записваема от потребител 10001."""
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        os.chown(workspace, RUNNER_UID, RUNNER_UID)
    except (PermissionError, AttributeError, OSError):
        workspace.chmod(0o777)


def run_task(text: str, workspace: Path, *, limits: Limits | None = None,
             on_event: Callable[[dict[str, Any]], None] | None = None,
             env_file: str | None = None, runtime: str | None = None,
             image: str = IMAGE) -> Result:
    limits = limits or Limits()
    name = f"genesis-task-{uuid.uuid4().hex[:12]}"
    prepare_workspace(workspace)
    argv = docker_args(name, workspace, text, limits=limits, env_file=env_file,
                       runtime=runtime, image=image)
    t0 = time.monotonic()
    timed_out = threading.Event()

    def _watchdog() -> None:
        timed_out.set()
        subprocess.run(["docker", "kill", name], capture_output=True, check=False)

    timer = threading.Timer(limits.seconds, _watchdog)
    timer.start()
    events: list[dict[str, Any]] = []
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue  # шум от Python/rich — не е събитие
            if isinstance(ev, dict) and "kind" in ev:
                events.append(ev)
                if on_event:
                    on_event(ev)
        stderr = proc.stderr.read() if proc.stderr else ""
        code = proc.wait()
    finally:
        timer.cancel()

    done = next((e for e in reversed(events) if e.get("kind") == "done"), None)
    seconds = round(time.monotonic() - t0, 1)
    if timed_out.is_set():
        return Result(False, True, code, seconds, error=f"прекъснато след {limits.seconds} s",
                      events=events)
    if done is None:
        return Result(False, False, code, seconds,
                      error=(stderr.strip().splitlines() or ["контейнерът спря без резултат"])[-1][:500],
                      events=events)
    return Result(bool(done.get("ok")) and code == 0, False, code, seconds,
                  tokens=dict(done.get("tokens") or {}), error=str(done.get("error") or ""),
                  events=events)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Една задача в затворен контейнер.")
    p.add_argument("text")
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument("--seconds", type=int, default=Limits.seconds)
    p.add_argument("--env-file", help="API ключовете за моделите (KEY=value на ред)")
    p.add_argument("--runtime", help="напр. runsc (gVisor)")
    a = p.parse_args(argv)
    res = run_task(a.text, a.workspace, limits=Limits(seconds=a.seconds),
                   on_event=lambda e: print(json.dumps(e, ensure_ascii=False), flush=True),
                   env_file=a.env_file, runtime=a.runtime)
    print(json.dumps({"kind": "result", "ok": res.ok, "timed_out": res.timed_out,
                      "exit_code": res.exit_code, "seconds": res.seconds,
                      "tokens": res.tokens, "error": res.error}, ensure_ascii=False))
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
