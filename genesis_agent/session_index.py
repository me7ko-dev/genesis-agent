"""
genesis_agent.session_index — разговорите на всяка папка: последният за
`genesis -c` (продължи оттам, където спря — като `claude -c`) и всички за
`/resume` / `genesis -r` (избери по-стар — като `claude --resume`).

Сесиите са в общата папка с история (session_*.json); тук се пази само
папка → имена на файлове: `by_workspace.json` (последният, форматът отпреди
2026-10-15 — по-стар Genesis го чете както досега) и `by_workspace_all.json`
(всички, най-новият накрая).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

INDEX_NAME = "by_workspace.json"
ALL_NAME = "by_workspace_all.json"
_MAX_ENTRIES = 500
# На папка: /resume показва най-новите; по-старите файлове остават в историята
# (и в /history), само не се предлагат тук.
_MAX_PER_WORKSPACE = 100


def _key(workspace: Path) -> str:
    return os.path.normcase(os.path.realpath(workspace))


def _load(history_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((history_dir / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(data, dict) else {}


def _load_all(history_dir: Path) -> dict[str, list[str]]:
    try:
        data = json.loads((history_dir / ALL_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: [n for n in v if isinstance(n, str)] for k, v in data.items()
            if isinstance(k, str) and isinstance(v, list)}


def _write(history_dir: Path, name: str, data: dict) -> None:
    # Името на временния файл е на процеса: два чата в различни папки пишат
    # индекса едновременно и общото `.tmp` можеше да размени съдържанието им.
    tmp = history_dir / f"{name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, history_dir / name)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def record(history_dir: Path, workspace: Path, session_file: Path) -> None:
    """Запомня, че последният разговор в `workspace` е `session_file`."""
    key = _key(workspace)
    index = _load(history_dir)
    index.pop(key, None)
    index[key] = session_file.name
    # Най-новите — накрая; старите папки отпадат, за да не расте без край.
    _write(history_dir, INDEX_NAME, dict(list(index.items())[-_MAX_ENTRIES:]))

    every = _load_all(history_dir)
    names = [n for n in every.pop(key, []) if n != session_file.name]
    names.append(session_file.name)
    every[key] = names[-_MAX_PER_WORKSPACE:]
    _write(history_dir, ALL_NAME, dict(list(every.items())[-_MAX_ENTRIES:]))


def _safe(history_dir: Path, name: str) -> Path | None:
    # Само име в същата папка — не път: индексът не може да насочи към чужд файл.
    if not name or Path(name).name != name or not name.startswith("session_"):
        return None
    path = history_dir / name
    return path if path.is_file() else None


def latest(history_dir: Path, workspace: Path) -> Path | None:
    """Файлът на последния разговор в тази папка, ако още го има."""
    return _safe(history_dir, _load(history_dir).get(_key(workspace), ""))


def sessions(history_dir: Path, workspace: Path) -> list[Path]:
    """Всички запазени разговори на тази папка, които още ги има — най-новият
    (по последна промяна) пръв. Разговорите на други папки не влизат."""
    key = _key(workspace)
    names = list(_load_all(history_dir).get(key, []))
    # Отпреди 2026-10-15 се помнеше само последният — и той е разговор на папката.
    legacy = _load(history_dir).get(key)
    if legacy and legacy not in names:
        names.append(legacy)
    found = {p for p in (_safe(history_dir, n) for n in names) if p is not None}

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(found, key=lambda p: (mtime(p), p.name), reverse=True)


def read(path: Path) -> list[dict]:
    """Съобщенията на запазен разговор. ValueError — файлът не е разговор
    (валиден JSON, но не списък от съобщения, сриваше старта — одит 2026-10-09)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except UnicodeDecodeError as e:
        raise ValueError(f"не е UTF-8 ({e.reason})") from e
    if not isinstance(data, list) or not all(isinstance(m, dict) for m in data):
        raise ValueError("не е списък от съобщения")
    return data


@dataclass
class Entry:
    """Ред от списъка на /resume."""
    path: Path
    modified: float
    first: str        # първото съобщение на оператора, съкратено
    count: int        # реплики (оператор + модел), без служебните


def _short(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def describe(path: Path, *, limit: int = 70) -> Entry:
    """Датата, първото съобщение и броят реплики. ValueError/OSError — повреден."""
    data = read(path)
    convo = [m for m in data if m.get("role") in ("user", "assistant")]
    first = next((str(m.get("content")) for m in convo
                  if m.get("role") == "user" and isinstance(m.get("content"), str)
                  and m.get("content", "").strip()), "")
    return Entry(path, path.stat().st_mtime, _short(first, limit) if first else "(без съобщение)",
                 len(convo))


def entries(history_dir: Path, workspace: Path, *, exclude: Path | None = None,
            limit: int = 20) -> tuple[list[Entry], int]:
    """Редовете за /resume (най-новият пръв) и колко файла не се четат.

    `exclude` е текущият разговор — да го „продължиш“ няма смисъл. Празните
    (само системният промпт) не се показват."""
    out: list[Entry] = []
    bad = 0
    for path in sessions(history_dir, workspace):
        if exclude is not None and path == exclude:
            continue
        try:
            entry = describe(path)
        except (OSError, ValueError, TypeError, AttributeError):
            bad += 1
            continue
        if entry.count:
            out.append(entry)
        if len(out) >= limit:
            break
    return out, bad
