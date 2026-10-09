"""
genesis_agent.session_index — кой е последният разговор във всяка папка, за
`genesis -c` (продължи оттам, където спря — като `claude -c`).

Сесиите са в общата папка с история (session_*.json); тук се пази само
папка → име на файла, в `by_workspace.json` до тях.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

INDEX_NAME = "by_workspace.json"
_MAX_ENTRIES = 500


def _key(workspace: Path) -> str:
    return os.path.normcase(os.path.realpath(workspace))


def _load(history_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((history_dir / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(data, dict) else {}


def record(history_dir: Path, workspace: Path, session_file: Path) -> None:
    """Запомня, че последният разговор в `workspace` е `session_file`."""
    index = _load(history_dir)
    index.pop(_key(workspace), None)
    index[_key(workspace)] = session_file.name
    # Най-новите — накрая; старите папки отпадат, за да не расте без край.
    items = list(index.items())[-_MAX_ENTRIES:]
    tmp = history_dir / (INDEX_NAME + ".tmp")
    try:
        tmp.write_text(json.dumps(dict(items), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, history_dir / INDEX_NAME)
    except OSError:
        pass


def latest(history_dir: Path, workspace: Path) -> Path | None:
    """Файлът на последния разговор в тази папка, ако още го има."""
    name = _load(history_dir).get(_key(workspace))
    # Само име в същата папка — не път: индексът не може да насочи към чужд файл.
    if not name or Path(name).name != name or not name.startswith("session_"):
        return None
    path = history_dir / name
    return path if path.is_file() else None
