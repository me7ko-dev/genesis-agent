"""
genesis_agent.project_instructions — the operator's standing instructions for
a project, read at the start of every session (what CLAUDE.md is to Claude
Code, AGENTS.md to Codex).

Without it every session starts from zero: "tests are run with `make test`",
"answer in Bulgarian", "never touch migrations/" — said again, every time, or
forgotten. With it they are in the system prompt before the first word.

Where it is looked for (later = closer = wins when they disagree, so it comes
last in the prompt):
  1. ~/.genesis/GENESIS.md                      — the operator, every project
  2. each folder from the repository root down to the workspace:
       GENESIS.md, else AGENTS.md, else CLAUDE.md  (one per folder: many repos
       keep AGENTS.md as a link to CLAUDE.md and the text would come twice)
       GENESIS.local.md                          — personal, not committed
`@path/to/file.md` inside one of them pulls that file in (relative to the file
that names it, at most 3 levels deep) — only from inside the project or
~/.genesis and never a key or secret (sandbox.sensitive_path_reason).

Total size is capped: this goes into EVERY request.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

NAMES = ("GENESIS.md", "AGENTS.md", "CLAUDE.md")
LOCAL_NAME = "GENESIS.local.md"
_MAX_FILE = 8000
_MAX_TOTAL = 16000
_MAX_DEPTH = 3
_MAX_LEVELS = 6
# `@docs/style.md` — not an e-mail (`a@b.c`), not inside backticks.
_IMPORT = re.compile(r"(?<![\w`@])@((?:~|\.{1,2})?[\w./\\-]*[\w-]\.[A-Za-z0-9]+)\b")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass
class Source:
    path: Path
    text: str
    truncated: bool = False


def _home() -> Path:
    from genesis_agent.paths import GENESIS_HOME
    return Path(GENESIS_HOME)


def _project_root(workspace: Path) -> Path:
    """The repository root above the workspace (the folder with .git), or the
    workspace itself. The home folder is never a project root: a dotfiles
    repository in ~ made all of ~ "the project" — every config file there
    importable, ~/.genesis/hooks.json a "project" file (audit 2026-10-08)."""
    home = Path.home().resolve()
    cur = workspace
    for _ in range(_MAX_LEVELS):
        if cur == home or cur.parent == cur:
            break
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return workspace


def _folders(workspace: Path) -> list[Path]:
    root = _project_root(workspace)
    chain = [workspace]
    cur = workspace
    while cur != root and cur.parent != cur and len(chain) < _MAX_LEVELS:
        cur = cur.parent
        chain.append(cur)
    return list(reversed(chain))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _under(path: Path, roots: list[Path]) -> bool:
    return any(path == r or r in path.parents for r in roots)


def _allowed(path: Path, roots: list[Path]) -> Path | None:
    """The real path when `path` is a readable instruction file inside `roots`.

    The text of the path is checked BEFORE the filesystem is touched: on Windows
    resolving `\\\\host\\share\\x.md` already opens it over SMB (and hands the
    machine's NTLM hash to that host). Then the real path — through symlinks —
    must still be inside, a regular file, and not a key or secret: a cloned
    repository's `GENESIS.md -> /proc/self/environ` or `-> ~/.ssh/id_rsa` put
    those into the prompt sent to the provider (audit 2026-10-08)."""
    from genesis_agent.sandbox import sensitive_path_reason
    text = str(path)
    if text.startswith(("\\\\", "//")) or re.match(r"^[A-Za-z]:[^\\/]", text):
        return None
    if not _under(Path(os.path.normpath(text)), roots):
        return None
    try:
        real = path.resolve()
    except OSError:
        return None
    if not _under(real, roots) or sensitive_path_reason(str(real)):
        return None
    if str(real).startswith(("/proc/", "/dev/", "/sys/")):
        return None
    try:
        return real if real.is_file() else None
    except OSError:
        return None


def _expand(text: str, base: Path, roots: list[Path], depth: int, seen: set[Path]) -> str:
    """Append every `@file` the text names (outside code blocks)."""
    if depth >= _MAX_DEPTH:
        return text
    found: list[tuple[str, Path]] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        for m in _IMPORT.finditer(line):
            raw = m.group(1)
            target = Path(raw).expanduser() if raw.startswith("~") else base / raw
            if all(target != t for _, t in found):
                found.append((raw, target))
    extra = []
    for raw, target in found:
        real = _allowed(target, roots)
        if real is None or real in seen:
            continue
        seen.add(real)
        body = _expand(_read(real)[:_MAX_FILE], real.parent, roots, depth + 1, seen)
        if body.strip():
            extra.append(f"\n\n### @{raw}\n{body.strip()}")
    return text + "".join(extra)


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def load(workspace: str | Path) -> list[Source]:
    """The instruction files for this workspace, outermost first."""
    ws = Path(workspace).expanduser()
    try:
        ws = ws.resolve()
    except OSError:
        return []
    home = _resolved(_home())
    project = [_project_root(ws)]
    # (кандидат, откъде може да внася): файлът на оператора — от ~/.genesis;
    # файловете на проекта — само от проекта (не и от ~/.genesis: там е ключът
    # за телефона, логовете, уменията — одит 2026-10-08).
    candidates: list[tuple[Path, list[Path]]] = [(home / "GENESIS.md", [home])]
    for folder in _folders(ws):
        main = next((folder / n for n in NAMES if (folder / n).is_file()), None)
        if main is not None:
            candidates.append((main, project))
        candidates.append((folder / LOCAL_NAME, project))
    texts: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for path, roots in candidates:
        real = _allowed(path, roots)
        if real is None or real in seen:
            continue
        seen.add(real)
        text = _read(real)
        if text.strip():
            texts.append((path, _expand(text, real.parent, roots, 0, seen)))
    # Таванът се пълни от най-близкия файл навън: той печели при разминаване и
    # не бива да е този, който изпада (одит 2026-10-08: глобален 8K + корен 9K
    # изхвърляха „не пипай migrations/“ на папката без следа).
    budget = _MAX_TOTAL
    kept: list[Source] = []
    for path, text in reversed(texts):
        room = min(_MAX_FILE * 2, budget)
        if room <= 0:
            kept.append(Source(path, "", True))
            continue
        truncated = len(text) > room
        text = text[:room]
        budget -= len(text)
        kept.append(Source(path, text.strip(), truncated))
    return list(reversed(kept))


def prompt_section(workspace: str | Path) -> str:
    """The text for the system prompt, or "" when there is nothing."""
    sources = load(workspace)
    if not sources:
        return ""
    parts = [("## ИНСТРУКЦИИ ЗА ПРОЕКТА (от оператора — следвай ги пред общите правила; "
              "по-близкият до папката файл печели при разминаване)")]
    for s in sources:
        note = " (отрязано — файлът е по-дълъг)" if s.truncated else ""
        parts.append(f"### {s.path}{note}\n{s.text}")
    return "\n\n".join(parts)


INIT_PROMPT = """Разгледай този проект и напиши файл GENESIS.md в корена му — инструкциите,
които всяка следваща сесия ще чете преди първата дума. Кратко (до ~60 реда), само
неща, които не личат от един поглед върху файловете:

- какво е проектът в едно-две изречения;
- как се инсталира, пуска, тества и проверява (точните команди — провери ги, като
  ги пуснеш, ако е безопасно);
- структурата: къде е кое (само важното);
- правилата на кода тук: стил, именуване, какво не се пипа, особености;
- нещо, което би те подвело, ако не го знаеш.

Ако вече има GENESIS.md, AGENTS.md или CLAUDE.md — прочети го и го подобри, не
го пиши наново от нулата. Не измисляй команди, които не съществуват."""
