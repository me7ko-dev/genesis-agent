"""
genesis_agent.context_usage — /context: с какво е пълен контекстът (2026-10-10).

Като `/context` в Claude Code: системният промпт по части (основният, средата,
инструкциите на проекта, паметта), схемите на инструментите (вградени, MCP,
AGENT), разговорът (съобщения, отговори, изходи на инструменти) и колко остава.
Защо: заявката порасна с 21% от 5-те нови инструмента (PR #52) и операторът
нямаше как да види кое тежи, без да чете кода. Броенето е в знаци, а токените —
оценка (~4 знака на токен, както статус реда); истинският размер на последната
заявка идва от доставчика и се показва до нея, когато го има.
"""
from __future__ import annotations

import json
import re
from typing import Any

_CHARS_PER_TOKEN = 4
# Разделите, които build_system_prompt слага — точен списък. Одит 2026-10-10:
# по „главни букви“ „## Под-агенти“ и „## MCP инструменти“ се сливаха с горния
# раздел, а „## ПРАВИЛА“ от GENESIS.md на оператора се броеше за раздел на Genesis.
_SECTIONS = ("СРЕДАТА", "ИНСТРУКЦИИ ЗА ПРОЕКТА", "Под-агенти", "MCP инструменти",
             "СЪСТОЯНИЕ НА РАБОТАТА", "Скорошна активност", "ЗНАНИЕВ ГРАФ")
_SECTION = re.compile(r"^## (" + "|".join(map(re.escape, _SECTIONS)) + r")\b.*$", re.MULTILINE)


def _chars(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False))


def _k(chars: int) -> str:
    tokens = chars // _CHARS_PER_TOKEN
    return f"~{tokens / 1000:.1f}K" if tokens >= 1000 else f"~{tokens}"


def _title(heading: str) -> str:
    text = heading.split("(")[0].strip().rstrip(":—- ")
    return text[:40] or heading[:40]


def system_sections(prompt: str) -> list[tuple[str, int]]:
    """(име, знаци) за всеки раздел на системния промпт, в реда им."""
    starts = list(_SECTION.finditer(prompt))
    parts: list[tuple[str, int]] = []
    first = starts[0].start() if starts else len(prompt)
    if first:
        parts.append(("основни правила", first))
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(prompt)
        parts.append((_title(m.group(1)), end - m.start()))
    return parts


def _tool_name(schema: Any) -> str:
    if isinstance(schema, dict):
        fn = schema.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "?")
        return str(schema.get("name") or "?")
    return "?"


def _text(content: Any) -> int:
    if isinstance(content, list):   # части (текст + картинка)
        return sum(_chars(p.get("text")) if isinstance(p, dict) and "text" in p else _chars(p)
                   for p in content)
    return _chars(content)


def report(messages: Any, tools: list | None = None, window: int = 0,
           last_tokens: int = 0) -> str:
    msgs = [m for m in list(messages or []) if isinstance(m, dict)]
    # Промптът е само първото съобщение. По-късните „system“ са от хода:
    # изходи на инструменти в текстов режим („[Резултат]:“), бележките на цикъла
    # и резюмето от /compact — броени бяха като инструкции за проекта (одит 2026-10-10).
    system = str(msgs[0].get("content") or "") if msgs and msgs[0].get("role") == "system" else ""
    rest = msgs[1:] if msgs and msgs[0].get("role") == "system" else msgs

    names: dict[str, str] = {}
    user = assistant = results = notes = 0
    n_user = n_assistant = n_results = n_notes = 0
    biggest = ("", 0)
    for m in rest:
        role = m.get("role")
        if role == "assistant":
            n_assistant += 1
            assistant += _text(m.get("content")) + _chars(m.get("tool_calls"))
            for call in m.get("tool_calls") or []:
                if isinstance(call, dict):
                    fn = call.get("function") or {}
                    names[str(call.get("id"))] = str(fn.get("name") or "?")
        elif role == "tool" or (role == "system" and str(m.get("content") or "").startswith("[Резултат")):
            n_results += 1
            size = _text(m.get("content"))
            results += size
            if size > biggest[1]:
                biggest = (m.get("name") or names.get(str(m.get("tool_call_id")))
                           or ("[Резултат]" if role == "system" else "?"), size)
        elif role == "system":
            n_notes += 1
            notes += _text(m.get("content"))
        else:
            n_user += 1
            user += _text(m.get("content"))

    tools = list(tools or [])
    builtin = [t for t in tools if not _tool_name(t).startswith("mcp__") and _tool_name(t) != "AGENT"]
    mcp = [t for t in tools if _tool_name(t).startswith("mcp__")]
    agent = [t for t in tools if _tool_name(t) == "AGENT"]
    tool_chars = _chars(tools) if tools else 0
    talk = user + assistant + results + notes
    total = len(system) + tool_chars + talk

    lines = []
    head = f"Контекст: {_k(total)} токена"
    if window:
        head += f" от {window / 1000:.0f}K ({min(100, total // _CHARS_PER_TOKEN * 100 // window)}%)"
    lines.append(head + f" — оценка по ~{_CHARS_PER_TOKEN} знака на токен")
    if last_tokens:
        lines.append(f"Последната заявка по доставчика: {last_tokens / 1000:.1f}K токена (с отговора)")

    def share(chars: int) -> str:
        return f"{chars * 100 // total}%" if total else "0%"

    lines.append(f"  Системен промпт   {_k(len(system)):>7}  {share(len(system)):>4}")
    for name, size in system_sections(system):
        lines.append(f"    {name:<34} {_k(size):>7}")
    lines.append(f"  Инструменти ({len(tools)})  {_k(tool_chars):>7}  {share(tool_chars):>4}")
    if tools:
        lines.append(f"    вградени {len(builtin)} {_k(_chars(builtin))}"
                     + (f", MCP {len(mcp)} {_k(_chars(mcp))}" if mcp else "")
                     + (f", AGENT {_k(_chars(agent))}" if agent else ""))
        top = sorted(builtin, key=_chars, reverse=True)[:3]
        lines.append("    най-тежки: " + ", ".join(f"{_tool_name(t)} {_k(_chars(t))}" for t in top))
    else:
        lines.append("    (без инструменти в този изглед)")
    lines.append(f"  Разговор          {_k(talk):>7}  {share(talk):>4}")
    lines.append(f"    твоите съобщения {n_user} {_k(user)}, отговори {n_assistant} {_k(assistant)}")
    if n_results:
        lines.append(f"    изходи на инструменти {n_results} {_k(results)}"
                     f" (най-голям: {biggest[0]} {_k(biggest[1])})")
    if n_notes:
        lines.append(f"    резюме и бележки {n_notes} {_k(notes)}")
    if window:
        free = max(0, window - total // _CHARS_PER_TOKEN)
        lines.append(f"  Свободно          {f'~{free / 1000:.1f}K':>7}")
        # /compact свива само разговора — при огромен CLAUDE.md не помага.
        if total // _CHARS_PER_TOKEN * 100 >= window * 60 and talk * 4 >= total:
            lines.append("Над 60% — /compact свива разговора до резюме.")
    return "\n".join(lines)
