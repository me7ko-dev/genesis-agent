"""
genesis_agent.shell_mode — `!команда` в чата: операторът пуска команда сам,
без модела (като `!` в Claude Code).

    ❯ !git status
    ❯ !pytest -q tests/test_x.py

Изходът се вижда веднага, а моделът го получава в следващото съобщение — може
да се пита „защо пада?“ без копиране. Командата е на оператора, не на агента:
не минава през въпросите на sandbox-а (той пази от решенията на модела), но
има таван на времето и Ctrl-C я спира заедно с децата ѝ.
"""
from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

_TIMEOUT = 600          # s — после командата се спира
_MAX_CONTEXT = 8000     # знака от изхода, които стигат до модела (краят е най-важен)


def is_bang(text: str) -> bool:
    return text.startswith("!") and bool(text[1:].strip())


def _stop(proc: subprocess.Popen) -> None:
    from genesis_agent import sandbox
    try:
        sandbox.stop_process(proc)
    except Exception:
        proc.kill()


def run(command: str, workspace: Path, out: Callable[[str], None],
        timeout: float = _TIMEOUT) -> str:
    """Пуска командата в работната папка, показва изхода ред по ред и връща
    текста за модела (командата, кода и края на изхода)."""
    from genesis_agent import sandbox
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            sandbox._shell_argv(command), cwd=str(workspace), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except OSError as e:
        out(f"✗ {e}")
        return f"Операторът пусна `{command}` — не тръгна: {e}"

    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            lines.put(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
        lines.put(None)

    threading.Thread(target=pump, daemon=True, name="bang-output").start()
    collected: list[str] = []
    size = 0
    note = ""
    deadline = started + timeout
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                note = f"спряна след {int(timeout)} s"
                _stop(proc)
                break
            try:
                line = lines.get(timeout=min(left, 0.5))
            except queue.Empty:
                continue
            if line is None:
                break
            out(line)
            collected.append(line)
            size += len(line) + 1
            # Само краят се пази: сървър, който пише часове, не бива да пълни паметта.
            while size > _MAX_CONTEXT * 2 and len(collected) > 1:
                size -= len(collected.pop(0)) + 1
    except KeyboardInterrupt:
        note = "прекъсната (Ctrl-C)"
        _stop(proc)
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
    if proc.stdout is not None:
        proc.stdout.close()
    status = note or f"код {code}"
    out(f"[{status}, {time.monotonic() - started:.1f} s]")
    text = "\n".join(collected)
    if len(text) > _MAX_CONTEXT:
        text = "… [началото е отрязано]\n" + text[-_MAX_CONTEXT:]
    # Без ``` в изхода: оградата би се затворила рано и останалото би изглеждало
    # като инструкция от оператора, а не като изход.
    text = text.replace("```", "ʼʼʼ")
    return (f"Операторът пусна сам в терминала (папка {workspace}):\n"
            f"$ {command}\n[{status}]\n```\n{text}\n```")

