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
from typing import Any

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
    # Кодирането — както READ_FILE го разпознава (e2e 2026-10-11). Измерено:
    # `@lib.py` в cp1251 стигаше до модела като „NAME = '�������'“, а UTF-16
    # requirements.txt (`pip freeze >` в PowerShell) — „двоичен, не е прикачен“
    # заради нулевите байтове, докато READ_FILE чете и двата.
    from genesis_agent.code_edit import decode_text, displayable
    text, encoding = decode_text(raw[:_MAX_FILE], partial=len(raw) > _MAX_FILE)
    if b"\x00" in raw[:8192] and not encoding.startswith(("utf-16", "utf-32")):
        return None
    # CRLF → LF: файл от Windows иначе носи `\r` във всеки ред към модела
    # (CI на Windows, 2026-10-09) и харчи от тавана за нищо.
    text = displayable(text).removeprefix("﻿").replace("\r\n", "\n")
    if len(raw) > _MAX_FILE:
        text += f"\n… [отрязано на {_MAX_FILE} байта — за останалото READ_FILE с offset]"
    return text


_MAX_IMAGES = 4
# Път, пуснат в терминала с мишката (drag-and-drop) или поставен: в кавички,
# с `\ ` вместо интервал, или file:// адрес — и завършва на разширение на
# картинка. Без `@` се прикача само абсолютен път (или ~/, file://): „поправи
# logo.png“ в текста не е молба да се прати картинката (2026-10-15).
_DROPPED = re.compile(
    r"""(?:'(?P<sq>[^'\n]+)'|"(?P<dq>[^"\n]+)"|(?P<bare>(?:\\\ |[^\s'"`])+))""")


# Кодов участък в ред: `…`, ``…`` (еднакъв брой кавички отвори и затвори).
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)")


def _outside_code(text: str) -> str:
    """Текстът без ``` блоковете и `кода` в ред (2026-10-15, ревю на зрението):
    лог или скрипт, поставен в кодов блок, с `/home/me/shot.png` вътре
    прикачваше картинката, без операторът да я праща — пътят там е текст за
    модела, не пусната с мишката картинка. Редовете остават (празни)."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else _INLINE_CODE.sub(" ", line))
    return "\n".join(out)


def _dropped_paths(text: str) -> list[str]:
    from genesis_agent.vision import IMAGE_SUFFIXES
    found: list[str] = []
    for m in _DROPPED.finditer(_outside_code(text)):
        raw = m.group("sq") or m.group("dq") or (m.group("bare") or "").replace("\\ ", " ")
        if raw.startswith("@"):
            continue           # @път е работа на _mentions
        if raw.lower().startswith("file://"):
            from urllib.parse import unquote, urlparse
            raw = unquote(urlparse(raw).path)
            if re.match(r"^/[A-Za-z]:/", raw):   # file:///C:/… на Windows
                raw = raw[1:]
        if not (raw.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", raw)):
            continue
        if Path(raw).suffix.lower() in IMAGE_SUFFIXES and raw not in found:
            found.append(raw)
    return found


def _is_image_file(path: Path, sniff: Any) -> bool:
    """Картинка без разширение на картинка (`shot` от скрийншот инструмент)."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return False
        with open(path, "rb") as fh:
            return sniff(fh.read(16)) is not None
    except OSError:
        return False


def _image(path: Path, label: str, images: list[dict], notes: list[str]) -> None:
    from genesis_agent import vision
    if len(images) >= _MAX_IMAGES:
        notes.append(f"{label}: не е прикачена — най-много {_MAX_IMAGES} картинки наведнъж")
        return
    try:
        part = vision.load_image(path)
    except vision.ImageError as e:
        notes.append(f"{label}: картинката не е прикачена — {e}")
        return
    images.append(part)
    notes.append(f"{label}: прикачена картинка ({vision.describe(part)})")


def expand(text: str, workspace: Path,
           images: list[dict] | None = None) -> tuple[str, list[str]]:
    """(съобщението с прикачените файлове, бележки за оператора).

    `images` (2026-10-15): списък, в който картинките (`@shot.png` и пуснат с
    мишката абсолютен път) влизат като части за vision.user_message. Без него
    — старото поведение: картинката е двоичен файл и не се прикача."""
    from genesis_agent.sandbox import sensitive_path_reason
    from genesis_agent.vision import IMAGE_SUFFIXES, sniff_mime
    parts: list[str] = []
    notes: list[str] = []
    total = 0
    attached = 0
    seen: set[Path] = set()
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
        if images is not None and real.is_file() and (
                real.suffix.lower() in IMAGE_SUFFIXES or _is_image_file(real, sniff_mime)):
            seen.add(real)
            _image(real, f"@{raw}", images, notes)
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
    if images is not None:
        for raw in _dropped_paths(text):
            path = _target(raw, workspace)
            if path is None or not path.is_file():
                continue
            real = Path(os.path.realpath(path))
            if real in seen:
                continue
            seen.add(real)
            if sensitive_path_reason(str(real)):
                notes.append(f"{raw}: чувствителен файл — не е прикачен")
                continue
            _image(real, Path(raw).name, images, notes)
    if not parts:
        return text, notes
    return text + "\n\n---\nПрикачено от оператора:\n\n" + "\n\n".join(parts), notes
