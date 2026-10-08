"""
genesis_agent.plan_mode — look first, change nothing, come back with a plan
(Claude Code's plan mode).

`/plan` in the chat switches it on: the model may read, search and ask, and
every tool that changes something (writing, editing, commands, skills,
delegation, the browser) is refused in code, not only asked for in the prompt.
`/plan` again (or "изпълни") switches it off and the plan is carried out.
"""
from __future__ import annotations

# Only what looks: files, the code, the web, the operator, the task list.
READ_ONLY_TOOLS = frozenset({
    "READ_FILE", "LIST_DIR", "GLOB", "SEARCH_CODE", "REPO_MAP",
    "WEB_SEARCH", "RESEARCH", "ASK_USER", "TASK_LIST",
})

_on = False

PROMPT_NOTE = (
    "## РЕЖИМ ПЛАН (включен от оператора)\n"
    "Сега само ПРОУЧВАШ и ПЛАНИРАШ. Чети файлове, търси в кода, питай — но НЕ "
    "променяй нищо: записът, редакцията, командите и уменията са изключени и ще "
    "бъдат отказани. Завърши с план: какво точно ще промениш (файлове, функции), "
    "в какъв ред, как ще провериш, че работи, и какви са рисковете. Кратко и "
    "конкретно. Операторът ще го одобри с /plan (изключва режима) и тогава го изпълняваш."
)


def active() -> bool:
    return _on


def set_active(on: bool) -> None:
    global _on
    _on = bool(on)


def toggle() -> bool:
    set_active(not _on)
    return _on


def refusal(tool: str) -> str | None:
    """A refusal for a tool that would change something, while plan mode is on."""
    if not _on or tool in READ_ONLY_TOOLS:
        return None
    return (f"[{tool}] ⏸ Режим план: нищо не се променя, докато операторът не одобри "
            "плана (/plan). Довърши плана — какво ще направиш и как ще го провериш.")


def filter_tools(tools: list[dict] | None) -> list[dict] | None:
    """Only the read-only tool schemas while plan mode is on."""
    if not _on or not tools:
        return tools
    return [t for t in tools if (t.get("function") or {}).get("name") in READ_ONLY_TOOLS]
