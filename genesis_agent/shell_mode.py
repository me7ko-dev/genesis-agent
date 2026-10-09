"""
genesis_agent.shell_mode — `!команда` в чата: операторът пуска команда сам,
без модела (като `!` в Claude Code).

    ❯ !git status
    ❯ !pytest -q tests/test_x.py

Изходът се вижда веднага, а моделът го получава в следващото съобщение — може
да се пита „защо пада?“ без копиране. Командата е на оператора, не на агента:
не минава през въпросите на sandbox-а (той пази от решенията на модела), но
има таван на времето и Ctrl-C я спира заедно с децата ѝ. Фоновите процеси
(`!сървър &`) се спират, щом командата свърши — за тях е `RUN_BG` / `/bg`.
"""
from __future__ import annotations

import locale
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

_TIMEOUT = 600          # s — после командата се спира
_MAX_CONTEXT = 8000     # знака от изхода, които стигат до модела (краят е най-важен)
_CHUNK = 8192           # байта — и най-дългият ред, който се държи цял
_GRACE = 0.5            # s изход след края на обвивката — после фоновите деца се спират


def is_bang(text: str) -> bool:
    return text.startswith("!") and bool(text[1:].strip())


def _decode(raw: bytes) -> str:
    """UTF-8; на Windows програмите извън Python пишат в кодовата страница на
    системата (cp1251/cp866) — ред, който не е UTF-8, се чете с нея."""
    if sys.platform == "win32":
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode(locale.getpreferredencoding(False), errors="replace")
    return raw.decode("utf-8", errors="replace")


def _kill_group(proc: subprocess.Popen) -> None:
    """Спира командата и всичко, пуснато от нея — и когато самата обвивка вече
    е излязла: `sleep 100 &` остава в групата ѝ и държи изхода отворен (одит
    2026-10-09: sandbox.stop_process виждаше излязла обвивка и не спираше нищо,
    а чатът чакаше 600 s)."""
    if sys.platform != "win32":
        for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 0.0)):
            try:
                os.killpg(proc.pid, sig)   # start_new_session → групата е pid-ът
            except (ProcessLookupError, PermissionError):
                return
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                try:
                    os.killpg(proc.pid, 0)
                except (ProcessLookupError, PermissionError):
                    return
                time.sleep(0.05)
        return
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
            # Python дете на Windows иначе пише в cp1251 и кирилицата излиза
            # развалена — същото като sandbox._run (2026-09-20).
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            start_new_session=sys.platform != "win32",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except OSError as e:
        out(f"✗ {e}")
        return f"Операторът пусна `{command}` — не тръгна: {e}"

    lines: queue.Queue[str | None] = queue.Queue()
    fd = proc.stdout.fileno() if proc.stdout is not None else -1

    def pump() -> None:
        # os.read на парчета, не readline: изход без нов ред (`yes | tr -d '\n'`)
        # иначе трупаше стотици MB, а лентите за напредък с `\r` не се виждаха
        # до края (одит 2026-10-09).
        pending = b""
        while True:
            try:
                chunk = os.read(fd, _CHUNK)
            except OSError:
                chunk = b""
            if not chunk:
                break
            pending += chunk
            parts = pending.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
            pending = parts.pop()
            if len(pending) >= _CHUNK:
                parts.append(pending)
                pending = b""
            for part in parts:
                lines.put(_decode(part))
        if pending:
            lines.put(_decode(pending))
        lines.put(None)

    pumper = threading.Thread(target=pump, daemon=True, name="bang-output")
    pumper.start()
    collected: list[str] = []
    size = 0
    note = ""
    deadline = started + timeout
    exited_at: float | None = None
    last_line = started
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                note = f"спряна след {int(timeout)} s"
                break
            if exited_at is None and proc.poll() is not None:
                exited_at = now
            # Обвивката излезе и изходът мълчи — фоново дете държи тръбата
            # отворена. Мълчанието се мери от последния ред, за да не се
            # изгуби краят на голям изход, който още се изчита.
            if exited_at is not None and now - max(exited_at, last_line) > _GRACE:
                break
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                break
            last_line = time.monotonic()
            out(line)
            collected.append(line)
            size += len(line) + 1
            # Само краят се пази: сървър, който пише часове, не бива да пълни паметта.
            while size > _MAX_CONTEXT * 2 and len(collected) > 1:
                size -= len(collected.pop(0)) + 1
    except KeyboardInterrupt:
        note = "прекъсната (Ctrl-C)"
    _kill_group(proc)
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
    # Затваря се само когато четящата нишка е свършила: затварянето чакаше
    # нейния readline и блокираше и Ctrl-C (одит 2026-10-09).
    pumper.join(timeout=1)
    if not pumper.is_alive() and proc.stdout is not None:
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
