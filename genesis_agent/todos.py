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
    # Първо копие на списъка (атомарно), после на елементите: телефонът чете
    # от HTTP нишката, докато ходът пише — обхождането на живия списък
    # смесваше стария и новия (67 от 221 608 четения, одит 2026-10-09).
    return [dict(i) for i in _items[:]]


def clear() -> None:
    _items.clear()


def _status(raw: Any) -> str:
    word = str(raw or "pending").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"done": "completed", "complete": "completed", "todo": "pending",
               "doing": "in_progress", "active": "in_progress", "in_work": "in_progress"}
    return aliases.get(word, word)


def parse_text(arg: str) -> list[dict[str, str]] | None:
    """`[TODO_WRITE: …]`: JSON списък, `{"todos": […]}` или редове `[ ] …` /
    `[~] …` / `[x] …`. None — JSON обект, който не е списък със задачи."""
    arg = arg.strip()
    try:
        loaded = json.loads(arg)
        # Формата на native схемата (`{"todos": […]}`) и в текстов режим (bug
        # fix, 2026-10-11): дотук ставаше ЕДНА задача със суровия JSON за текст
        # и подменяше целия списък. Друг JSON обект се отказва, не се гадае.
        if isinstance(loaded, dict):
            loaded = loaded.get("todos")
            if not isinstance(loaded, list):
                return None
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


# Как моделите наричат текста и „готово“ — не само схемата. Проба на
# приложението 2026-10-12: gpt-oss-120b пишеше {"description", "done": false}.
_TEXT_KEYS = ("content", "task", "title", "text", "description", "name", "step")
_DONE_KEYS = ("done", "completed", "checked")


def _item(raw: dict) -> tuple[str, Any]:
    text = next((raw[k] for k in _TEXT_KEYS if raw.get(k)), "")
    status = raw.get("status")
    if status is None:
        done = next((raw[k] for k in _DONE_KEYS if isinstance(raw.get(k), bool)), None)
        status = None if done is None else ("completed" if done else "pending")
    return " ".join(str(text).split())[:_MAX_TEXT], status


_UNREAD = ("[TODO_WRITE] ❌ Не разпознах нито една задача — всяка е "
           '{"content": "…", "status": "pending|in_progress|completed"}. Списъкът е непроменен.')
_CHECKBOX = re.compile(r"^\s*(?:[-*•]\s*)?\[[ xX✓~>\-]\]", re.MULTILINE)


def _is_list_text(arg: str) -> bool:
    """JSON списък/обект или редове с отметки — не произволен низ."""
    try:
        return isinstance(json.loads(arg), (list, dict))
    except ValueError:
        return bool(_CHECKBOX.search(arg))


def write(todos: Any, *, native: bool = False) -> str:
    """Подменя целия списък. Връща го нарисуван (или защо е отказан).

    `native` — низ в полето `todos` на native извикването: приема се само JSON
    или редове с отметки (`[ ]`/`[x]`)."""
    if isinstance(todos, str):
        # Празен таг (`[TODO_WRITE: ]`) от слаб модел изтриваше целия списък,
        # а `{"todos": "update"}` го подменяше с една задача „update“ (одит
        # 2026-10-22) — същата грешка като липсващия ключ (2026-10-12): отказ.
        # Чисти само изричното `[]`.
        if not todos.strip() or (native and not _is_list_text(todos)):
            return _UNREAD
        todos = parse_text(todos)
    if not isinstance(todos, list):
        return "[TODO_WRITE] ❌ Очаква се списък: [{content, status}]."
    new: list[dict[str, str]] = []
    for raw in todos[:_MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        text, raw_status = _item(raw)
        status = _status(raw_status)
        if not text:
            continue
        if status not in STATUSES:
            return (f"[TODO_WRITE] ❌ Непознат статус „{raw_status}“ — "
                    f"само {', '.join(STATUSES)}.")
        new.append({"content": text, "status": status})
    if todos and not new:
        # Задачи имаше, но нито една не се прочете: не трием списъка мълчаливо
        # („празен“ караше модела да опитва пак и пак, 12 пъти в един ход).
        return _UNREAD
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

