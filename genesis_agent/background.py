"""
genesis_agent.background — commands that keep running while the agent works
(Claude Code's background Bash): a dev server, a watcher, a long build.

RUN_CMD with background=true (text tag: [RUN_BG: command]) starts it through
the same sandbox gate as any command and returns an id at once. BG_OUTPUT id
gives what it printed since the last look (and whether it still runs), BG_KILL
id stops it with its children. Everything still running stops when the chat
ends. `/bg` in the chat lists them.
"""
from __future__ import annotations

import atexit
import itertools
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import Popen

MAX_JOBS = 5
_MAX_OUTPUT = 6000


@dataclass
class Job:
    id: str
    command: str
    proc: Popen
    log: Path
    started: float = field(default_factory=time.time)
    read_to: int = 0


_jobs: dict[str, Job] = {}
_ids = itertools.count(1)
_dir: Path | None = None


def _log_dir() -> Path:
    global _dir
    if _dir is None:
        _dir = Path(tempfile.mkdtemp(prefix="genesis-bg-"))
    return _dir


def _running() -> list[Job]:
    return [j for j in _jobs.values() if j.proc.poll() is None]


def start(command: str, cwd: Path | None = None) -> str:
    from genesis_agent import sandbox
    command = command.strip()
    if not command:
        return "[RUN_BG] ❌ Празна команда."
    if len(_running()) >= MAX_JOBS:
        return (f"[RUN_BG] ❌ Вече вървят {MAX_JOBS} фонови команди — спри някоя с BG_KILL "
                "(списък: BG_OUTPUT без id).")
    job_id = f"bg{next(_ids)}"
    log = _log_dir() / f"{job_id}.log"
    proc, refusal = sandbox.start_shell(command, cwd=cwd, log=log)
    if proc is None:
        return f"[RUN_BG: {command}] {refusal}"
    _jobs[job_id] = Job(job_id, command, proc, log)
    time.sleep(1.0)  # грешка при старта (порт зает, липсваща команда) се вижда веднага
    first = output(job_id)
    return (f"[RUN_BG: {command}] ▶ пуснато във фона като {job_id}. Изходът: BG_OUTPUT {job_id}; "
            f"спиране: BG_KILL {job_id}.\n{first}")


def output(job_id: str = "") -> str:
    job_id = job_id.strip()
    if not job_id:
        return summary()
    job = _jobs.get(job_id)
    if job is None:
        return f"[BG_OUTPUT] ❌ Няма фонова команда {job_id}. {summary()}"
    try:
        data = job.log.read_bytes()
    except OSError:
        data = b""
    new = data[job.read_to:]
    job.read_to = len(data)
    text = new.decode("utf-8", errors="replace")
    if len(text) > _MAX_OUTPUT:
        text = f"… [пропуснати {len(text) - _MAX_OUTPUT} знака] …\n" + text[-_MAX_OUTPUT:]
    code = job.proc.poll()
    state = "върви" if code is None else f"приключи с код {code}"
    return f"[BG_OUTPUT: {job_id}] ({state}; {job.command})\n{text or '(нищо ново)'}"


def kill(job_id: str) -> str:
    from genesis_agent import sandbox
    job = _jobs.get(job_id.strip())
    if job is None:
        return f"[BG_KILL] ❌ Няма фонова команда {job_id}. {summary()}"
    sandbox.stop_process(job.proc)
    return f"[BG_KILL: {job.id}] ■ спряна ({job.command})"


def summary() -> str:
    if not _jobs:
        return "Няма фонови команди."
    lines = []
    for j in _jobs.values():
        code = j.proc.poll()
        state = "върви" if code is None else f"код {code}"
        lines.append(f"{j.id}: {state}, {int(time.time() - j.started)} s — {j.command}")
    return "Фонови команди:\n" + "\n".join(lines)


@atexit.register
def stop_all() -> None:
    from genesis_agent import sandbox
    for job in _running():
        sandbox.stop_process(job.proc)
