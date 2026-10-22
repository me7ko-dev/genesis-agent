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
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
# И българските/типографските кавички, многоточието и апострофите (одит
# 2026-10-22): „@src/api.py“ търсеше файла `src/api.py“`, не го намираше и
# мълчеше — операторът мислеше, че е прикачен. macOS сам подменя "" с “”.
_TRAIL = ".,;:!?)]}>“”„«»…‘’"
# `@src/api.py—после`, `@src/api.py:42`, `@src/api.py#L1-2` (място от pytest,
# компилатора или GitHub) — пак без бележка, а файлът е там (одит 2026-10-22).
# Прикача се целият файл.
_DASHES = re.compile(r"[—–]")
_LINE_SUFFIX = re.compile(r"(?::\d+){1,2}$|#L\d+(?:-L?\d+)?$")
_MAX_FILE = 60_000
_MAX_TOTAL = 150_000
_MAX_ENTRIES = 200
_MAX_MENTIONS = 10


def _code_lines(lines: list[str]) -> list[bool]:
    """Кои редове са в ``` блок (заедно с оградите му), по правилото на
    CommonMark: блокът се затваря само от ограда със същия знак и поне същата
    дължина. Одит 2026-10-22: `~~~` вътре в ``` блок (пример за markdown в
    инструкциите) „затваряше“ блока, а истинският край го отваряше наново —
    всичко след него се смяташе за код и @импортите там изпадаха."""
    inside: list[bool] = []
    fence = ""
    for line in lines:
        m = _FENCE.match(line)
        if not fence:
            # ``` с ` в инфо низа е код в ред (```x``` на един ред), не ограда.
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                fence = m.group(1)
            inside.append(bool(fence))
            continue
        if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                and not m.group(2).strip():
            fence = ""
        inside.append(True)
    return inside


def _mentions(text: str) -> list[str]:
    found: list[str] = []
    lines = text.splitlines()
    for line, fenced in zip(lines, _code_lines(lines)):
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


def _find(raw: str, workspace: Path) -> tuple[str, Path] | None:
    """(записът, пътят) за `raw` — или без залепеното след тире, или без :ред."""
    tries = [raw]
    cut = _DASHES.split(raw, maxsplit=1)[0].rstrip(_TRAIL)
    if cut and cut != raw:
        tries.append(cut)
    for t in list(tries):
        bare = _LINE_SUFFIX.sub("", t).rstrip(_TRAIL)
        if bare and bare not in tries:
            tries.append(bare)
    for t in tries:
        path = _target(t, workspace)
        if path is not None:
            return t, path
    return None


# Тайни, които образецът на sandbox-а не познава: той пази shell команди, където
# `\.key\b` би спрял и `jq .key`. Тук се гледа името на файла — както при
# `genesis export` (deliver._EXPORT_SECRETS) — плюс тези, които и там липсват.
# Одит 2026-10-22: `config/master.key` (Rails), `certs/server.key`,
# `.streamlit/secrets.toml`, `*.ppk`, `service-account.json`, `terraform.tfvars`,
# `.dev.vars` (Cloudflare), `*.jks` и `.envrc` (direnv с AWS ключове) стигаха до
# модела — по @път в чата и по @импорт в системния промпт на ВСЯКА заявка.
# Ревю 2026-10-22: с импортите без разширение `@kubeconfig` (токенът или
# client-key-data на клъстера) и `@.terraformrc` (токенът за Terraform Cloud)
# вече стигаха до промпта. `kubeconfig.go` е код — не се пипа.
_MODEL_SECRETS = re.compile(
    r"\.tfvars(?:\.json)?$|\.tfstate(?:\.backup)?$|(?:^|/)\.dev\.vars$"
    r"|(?:^|/)kubeconfig(?:\.ya?ml|\.json)?$|\.kubeconfig$|(?:^|/)(?:\.terraformrc|terraform\.rc)$",
    re.IGNORECASE)
# И по съдържание — частен ключ под невинно име (`deploy-notes.txt`). Само в
# началото на ред: изходен код с такъв низ в кавички (тест на парсер) не е ключ.
_PRIVATE_KEY = re.compile(r"^\s*-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
                          r"|^PuTTY-User-Key-File-\d|\"private_key\"\s*:\s*\"-----BEGIN",
                          re.MULTILINE)


def secret_reason(path: str | os.PathLike[str]) -> str | None:
    """Защо файлът не бива да стига до модела (ключ/тайна), или None."""
    from genesis_agent.deliver import _EXPORT_SECRETS
    from genesis_agent.sandbox import _SENSITIVE_PATH_EXEMPT_RE, sensitive_path_reason
    why = sensitive_path_reason(path)
    if why:
        return why
    text = os.fspath(path).replace("\\", "/")
    if _SENSITIVE_PATH_EXEMPT_RE.search(text):   # secrets.example и подобни
        return None
    m = _EXPORT_SECRETS.search(text) or _MODEL_SECRETS.search(text)
    return f"файл с тайни ({m.group(0).lstrip('/')})" if m else None


def has_private_key(text: str) -> bool:
    return _PRIVATE_KEY.search(text) is not None


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
    lines = text.splitlines()
    return "\n".join("" if fenced else _INLINE_CODE.sub(" ", line)
                     for line, fenced in zip(lines, _code_lines(lines)))


def _looks_like_path(raw: str) -> bool:
    return raw.startswith(("/", "~")) or raw.lower().startswith("file://") \
        or re.match(r"^[A-Za-z]:[\\/]", raw) is not None


def _dropped_paths(text: str) -> list[str]:
    from genesis_agent.vision import IMAGE_SUFFIXES
    found: list[str] = []
    # Кавички само около път („'Screen Shot.png'“ от терминала); „Here's /x.png,
    # what's wrong“ има два апострофа и гълташе пътя (2026-10-20) → тогава
    # текстът между тях се чете дума по дума. Пунктуацията след пътя („/x.png.“,
    # „(/x.png)“) пада като при @път.
    queue: list[str] = []
    for m in _DROPPED.finditer(_outside_code(text)):
        quoted = m.group("sq") or m.group("dq")
        if quoted and not _looks_like_path(quoted):
            queue.extend(t for t in quoted.split() if _looks_like_path(t.lstrip("(")))
            continue
        queue.append(quoted or (m.group("bare") or "").replace("\\ ", " "))
    for raw in queue:
        raw = raw.lstrip("(").rstrip(_TRAIL)
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
        found = _find(raw, workspace)
        if found is None:
            continue   # @property, имейл, несъществуващ път — не се броят
        raw, path = found
        real = Path(os.path.realpath(path))
        # По истинския път (одит 2026-10-22): `@src/api.py и @./src/api.py`
        # прикачваше файла два пъти и харчеше от тавана.
        if real in seen:
            continue
        seen.add(real)
        if attached >= _MAX_MENTIONS:
            notes.append(f"@{raw}: не е прикачен — най-много {_MAX_MENTIONS} наведнъж")
            continue
        attached += 1
        if secret_reason(str(real)):
            notes.append(f"@{raw}: чувствителен файл — не е прикачен")
            continue
        if images is not None and real.is_file() and (
                real.suffix.lower() in IMAGE_SUFFIXES or _is_image_file(real, sniff_mime)):
            _image(real, f"@{raw}", images, notes)
            continue
        if real.is_dir():
            body, kind = _listing(real), "папка"
        else:
            content = _read(real)
            if content is None:
                notes.append(f"@{raw}: двоичен, нечетим или не обикновен файл — не е прикачен")
                continue
            if has_private_key(content):
                notes.append(f"@{raw}: чувствителен файл (частен ключ) — не е прикачен")
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
            hit = _target(raw, workspace)
            if hit is None or not hit.is_file():
                continue
            real = Path(os.path.realpath(hit))
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
