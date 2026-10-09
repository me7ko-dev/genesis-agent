"""
genesis_agent.todos — списъкът със задачи на текущата работа (TodoWrite в
Claude Code): моделът го води, операторът го вижда.

При задача с няколко стъпки моделът пише целия списък с TODO_WRITE и го
обновява, докато върви — една задача „в работа“, готовите се отмятат веднага.
Така и операторът, и самият модел виждат докъде е стигнал. Живее в паметта на
сесията; /clear го чисти.
"""
from __future__ import annotations

import json
import re
from typing import Any

STATUSES = ("pending", "in_progress", "completed")
_MARK = {"pending": "☐", "in_progress": "▶", "completed": "☑"}
_MAX_ITEMS = 50
_MAX_TEXT = 300
_LINE = re.compile(r"^\s*(?:[-*•]\s*)?(?:\[(?P<mark>[ xX✓~>\-])\]\s*)?(?P<text>.+?)\s*$")
_SPLIT = re.compile(r";\s*(?=(?:[-*•]\s*)?\[[ xX✓~>\-]\])")
_FROM_MARK = {" ": "pending", "x": "completed", "X": "completed", "✓": "completed",
              "~": "in_progress", ">": "in_progress", "-": "in_progress"}

_items: list[dict[str, str]] = []


def items() -> list[dict[str, str]]:
    return [dict(i) for i in _items]


def clear() -> None:
    _items.clear()


def _status(raw: Any) -> str:
    word = str(raw or "pending").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"done": "completed", "complete": "completed", "todo": "pending",
               "doing": "in_progress", "active": "in_progress", "in_work": "in_progress"}
    return aliases.get(word, word)


def parse_text(arg: str) -> list[dict[str, str]]:
    """`[TODO_WRITE: …]`: JSON списък или редове `[ ] …` / `[~] …` / `[x] …`."""
    arg = arg.strip()
    try:
        loaded = json.loads(arg)
        if isinstance(loaded, list):
            return [i if isinstance(i, dict) else {"content": str(i)} for i in loaded]
    except ValueError:
        pass
    lines = [ln for ln in arg.splitlines() if ln.strip()]
    if len(lines) == 1:
        # Само `;` пред следваща отметка дели: `[~] пусни pytest -q; ruff check`
        # е една стъпка, не две (одит 2026-10-09).
        lines = [part for part in _SPLIT.split(lines[0]) if part.strip()]
    out = []
    for line in lines:
        m = _LINE.match(line)
        if m:
            out.append({"content": m.group("text"),
                        "status": _FROM_MARK.get(m.group("mark") or " ", "pending")})
    return out


def write(todos: Any) -> str:
    """Подменя целия списък. Връща го нарисуван (или защо е отказан)."""
    if isinstance(todos, str):
        todos = parse_text(todos)
    if not isinstance(todos, list):
        return "[TODO_WRITE] ❌ Очаква се списък: [{content, status}]."
    new: list[dict[str, str]] = []
    for raw in todos[:_MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        text = " ".join(str(raw.get("content") or raw.get("task") or "").split())[:_MAX_TEXT]
        status = _status(raw.get("status"))
        if not text:
            continue
        if status not in STATUSES:
            return (f"[TODO_WRITE] ❌ Непознат статус „{raw.get('status')}“ — "
                    f"само {', '.join(STATUSES)}.")
        new.append({"content": text, "status": status})
    _items[:] = new
    note = ""
    if sum(i["status"] == "in_progress" for i in new) > 1:
        note = "\n(Една задача в работа наведнъж — довърши я, после следващата.)"
    return render() + note


def render() -> str:
    if not _items:
        return "📋 Списъкът със задачи е празен."
    done = sum(i["status"] == "completed" for i in _items)
    lines = [f"📋 Задачи ({done}/{len(_items)} готови)"]
    lines += [f"{_MARK[i['status']]} {i['content']}" for i in _items]
    return "\n".join(lines)

