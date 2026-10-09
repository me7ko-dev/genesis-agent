"""
genesis_agent.mentions — `@път` в съобщението на оператора прикача файла (или
списъка на папката) към него, като `@` в Claude Code:

    ❯ защо пада @tests/test_api.py спрямо @src/api/

Пътят е спрямо работната папка (или абсолютен / `~/…`). Прикача се само
съществуващ файл или папка — `user@example.com` и `@decorator` остават текст.
Чувствителните файлове (.env, ключове) не се пращат на модела.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_MENTION = re.compile(r"(?<![\w@`])@(\"[^\"\n]+\"|[^\s`'\"]+)")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TRAIL = ".,;:!?)]}>"
_MAX_FILE = 60_000
_MAX_TOTAL = 150_000
_MAX_ENTRIES = 200
_MAX_MENTIONS = 10


def _mentions(text: str) -> list[str]:
    found: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        for m in _MENTION.finditer(line):
            raw = m.group(1)
            raw = raw[1:-1] if raw.startswith('"') else raw.rstrip(_TRAIL)
            if raw and raw not in found:
                found.append(raw)
    return found


def _target(raw: str, workspace: Path) -> Path | None:
    # `@~непознат` хвърляше RuntimeError и сваляше `genesis -p` (одит 2026-10-09).
    try:
        path = Path(raw).expanduser() if raw.startswith("~") else Path(raw)
        if not path.is_absolute():
            path = workspace / path
        return path if path.exists() else None
    except (OSError, ValueError, RuntimeError):
        return None


def _listing(folder: Path) -> str:
    try:
        entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as e:
        return f"(не се чете: {e})"
    shown = [f"{p.name}/" if p.is_dir() else p.name for p in entries[:_MAX_ENTRIES]
             if not p.name.startswith(".git")]
    more = len(entries) - _MAX_ENTRIES
    return "\n".join(shown) + (f"\n… и още {more}" if more > 0 else "")


def _read(path: Path) -> str | None:
    """Текстът на файла или None, ако е двоичен или не е обикновен файл."""
    try:
        # FIFO, /dev/tty и подобни: open() чакаше вечно, а единственият изход —
        # Ctrl-C — затваряше чата (одит 2026-10-09).
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_FILE + 1)
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    # CRLF → LF: файл от Windows иначе носи `\r` във всеки ред към модела
    # (CI на Windows, 2026-10-09) и харчи от тавана за нищо.
    text = raw[:_MAX_FILE].decode("utf-8", errors="replace").replace("\r\n", "\n")
    if len(raw) > _MAX_FILE:
        text += f"\n… [отрязано на {_MAX_FILE} байта — за останалото READ_FILE с offset]"
    return text


def expand(text: str, workspace: Path) -> tuple[str, list[str]]:
    """(съобщението с прикачените файлове, бележки за оператора)."""
    from genesis_agent.sandbox import sensitive_path_reason
    parts: list[str] = []
    notes: list[str] = []
    total = 0
    attached = 0
    for raw in _mentions(text):
        path = _target(raw, workspace)
        if path is None:
            continue   # @property, имейл, несъществуващ път — не се броят
        if attached >= _MAX_MENTIONS:
            notes.append(f"@{raw}: не е прикачен — най-много {_MAX_MENTIONS} наведнъж")
            continue
        attached += 1
        real = Path(os.path.realpath(path))
        if sensitive_path_reason(str(real)):
            notes.append(f"@{raw}: чувствителен файл — не е прикачен")
            continue
        if real.is_dir():
            body, kind = _listing(real), "папка"
        else:
            content = _read(real)
            if content is None:
                notes.append(f"@{raw}: двоичен, нечетим или не обикновен файл — не е прикачен")
                continue
            body, kind = content, "файл"
        if total + len(body) > _MAX_TOTAL:
            notes.append(f"@{raw}: не е прикачен — общият таван от {_MAX_TOTAL} знака е стигнат")
            continue
        total += len(body)
        # Оградата е по-дълга от всяка в съдържанието, за да не се затвори рано.
        fence = "`" * max(3, max((len(m) for m in re.findall(r"`{3,}", body)), default=0) + 1)
        parts.append(f"### @{raw} ({kind}: {real})\n{fence}\n{body}\n{fence}")
        notes.append(f"@{raw}: прикачен {kind} ({len(body)} знака)")
    if not parts:
        return text, notes
    return text + "\n\n---\nПрикачено от оператора:\n\n" + "\n\n".join(parts), notes
