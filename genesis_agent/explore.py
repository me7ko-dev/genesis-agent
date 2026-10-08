"""
genesis_agent.explore — a read-only sub-agent for questions about the code
(Claude Code's Explore agent).

"Where is the price computed and who calls it?" takes a dozen searches and
reads. Done in the main conversation, every file read stays in its history
and is paid for on every later request. EXPLORE runs that search in a fresh,
separate conversation with only the tools that look (READ_FILE, GLOB,
SEARCH_CODE, REPO_MAP, LIST_DIR) and hands back only the answer, with file
and line references. It cannot write, run commands or explore further
itself — the tools are not offered, and anything else is refused.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

TOOLS = ("READ_FILE", "GLOB", "SEARCH_CODE", "REPO_MAP", "LIST_DIR")
MAX_ROUNDS = 10
_MAX_ANSWER = 6000

PROMPT = """You are a read-only code explorer working for another agent. Answer ONE
question about the project in the workspace by searching and reading the code.

- Use SEARCH_CODE and GLOB to find things, READ_FILE (with offset/limit for big
  files) to confirm them. Several independent tool calls in one round are fine.
- You cannot change anything and you do not need to: no writing, no commands.
- Stop as soon as you can answer. Then reply with the answer only: concrete,
  with `path:line` references for every claim, the relevant snippets kept
  short. Say plainly what you could not find. No preamble, no plan.
- Answer in the language of the question.
- Your ONLY tools are READ_FILE, GLOB, SEARCH_CODE, REPO_MAP and LIST_DIR. Any
  other tool or tag you may see described (RUN_CMD, WRITE_FILE, …) is not
  yours and is ignored."""

# Аргументът носи скоби до две нива (`app/[id]/page.tsx`) — като _SIMPLE_RE.
_BRACKETS = r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]"
_TAG = re.compile(r"\[(READ_FILE|GLOB|SEARCH_CODE|REPO_MAP|LIST_DIR)(?::\s*((?:[^\[\]]|"
                  + _BRACKETS + r")*))?\]")
_MAX_CALLS = 8


def _schemas() -> list[dict]:
    from genesis_agent.tool_schemas import FULL_TOOLS
    return [t for t in FULL_TOOLS if t["function"]["name"] in TOOLS]


def _clip(text: str) -> str:
    try:
        from genesis_agent.budget import clip_for_context
        return clip_for_context(text)
    except Exception:
        return text[:8000]


def explore(question: str, workspace: str = "", *,
            complete: Callable[..., Any] | None = None,
            max_rounds: int = MAX_ROUNDS) -> str:
    """The answer to `question`, found in a separate read-only conversation."""
    import genesis_skills as gs
    question = question.strip()
    if not question:
        return "[EXPLORE] ❌ Няма въпрос."
    if complete is None:
        from genesis_agent.brain import Brain
        complete = Brain().complete
    system = PROMPT
    try:
        from genesis_agent.agent_core import env_facts
        system += "\n\n" + env_facts(workspace or str(gs._WORKSPACE))
    except Exception:
        pass
    messages: list[dict] = [{"role": "system", "content": system},
                            {"role": "user", "content": question}]
    # Какво е „видял“ главният агент не се мени от прочетеното тук: иначе след
    # EXPLORE WRITE_FILE презаписваше файл, който главният никога не е чел
    # (одит 2026-10-08).
    seen_before = set(gs._SEEN_PATHS)
    try:
        return _loop(question, messages, complete, max_rounds, gs)
    finally:
        gs._SEEN_PATHS.clear()
        gs._SEEN_PATHS.update(seen_before)


def _loop(question: str, messages: list[dict], complete: Callable[..., Any],
          max_rounds: int, gs: Any) -> str:
    schemas = _schemas()
    reads = 0
    last = ""
    for round_i in range(max_rounds):
        final = round_i == max_rounds - 1
        if final:  # последният рунд — без инструменти: отговор с каквото е намерено
            messages.append({"role": "user", "content":
                             "Стига търсене. Отговори сега с каквото намери (с път:ред)."})
        reply = complete(messages, tools=None if final else schemas)
        text = (getattr(reply, "raw_text", "") or "").strip()
        if text.startswith("Error:"):
            return f"[EXPLORE] ❌ {text[:300]}"
        last = text or last
        if final and not text:
            break
        calls = [] if final else (getattr(reply, "tool_calls", None) or [])
        if calls:
            calls = calls[:_MAX_CALLS]
            messages.append({"role": "assistant", "content": text, "tool_calls": calls})
            for tc in calls:
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                if name in TOOLS:
                    result = gs.dispatch_tool_call(name, fn.get("arguments") or {})
                    reads += 1
                else:
                    result = f"[{name}] ❌ Изследователят само чете: {', '.join(TOOLS)}."
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "name": name, "content": _clip(result)})
            continue
        tags = [] if final else list(_TAG.finditer(text))
        if tags:
            outs = []
            for m in tags[:_MAX_CALLS]:
                args = _text_args(m.group(1), (m.group(2) or "").strip())
                outs.append(_clip(gs.dispatch_tool_call(m.group(1), args)))
                reads += 1
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "[резултати]\n" + "\n\n".join(outs)})
            continue
        answer = text[:_MAX_ANSWER]
        return f"[EXPLORE: {question[:80]}] ({reads} прегледа)\n{answer}"
    return (f"[EXPLORE: {question[:80]}] спря след {max_rounds} рунда ({reads} прегледа) без "
            f"окончателен отговор. Последно:\n{last[:_MAX_ANSWER]}")


def _text_args(tool: str, arg: str) -> dict:
    if tool == "SEARCH_CODE":
        return {"pattern": arg}
    if tool == "GLOB":
        return {"pattern": arg}
    if tool == "READ_FILE":
        return {"path": arg}
    if tool == "LIST_DIR":
        return {"path": arg or "."}
    return {"path": arg}
