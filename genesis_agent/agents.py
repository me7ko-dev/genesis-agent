"""
genesis_agent.agents — собствени под-агенти (като `.claude/agents/` в Claude Code).

Един файл = един специалист с ролята си, инструментите си и отделен контекст:

    .genesis/agents/reviewer.md      ← в проекта (и `.claude/agents/` на Claude Code)
    ~/.genesis/agents/reviewer.md    ← за всеки проект

    ---
    name: reviewer
    description: Преглежда промяна за бъгове и липсващи тестове. Ползвай след всяка редакция.
    tools: READ_FILE, SEARCH_CODE, GLOB, RUN_CMD
    model: light            # по желание: light/haiku = малкият модел
    ---
    Ти си строг рецензент. Провери …

Главният агент вика `AGENT {agent, task}` (или `[AGENT: reviewer | задача]`);
под-агентът работи в свой разговор и връща само доклада. Инструментите му са
само изброените (без `tools` — всички без AGENT). Всяко негово действие минава
през същата бариера като на главния: режим план, hooks, sandbox, /undo.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_ROUNDS = 15
_MAX_CALLS = 8
_MAX_ANSWER = 8000
_MAX_PROMPT = 20_000
_MAX_AGENTS = 30
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")

# Имената от Claude Code → инструментите на Genesis: файл от `.claude/agents/`
# работи без преписване.
_CLAUDE_TOOLS = {
    "read": "READ_FILE", "write": "WRITE_FILE", "edit": "EDIT_FILE", "multiedit": "EDIT_FILE",
    "bash": "RUN_CMD", "grep": "SEARCH_CODE", "glob": "GLOB", "ls": "LIST_DIR",
    "webfetch": "WEB_FETCH", "websearch": "WEB_SEARCH", "todowrite": "TODO_WRITE",
}
# Под-агентът не вика под-агенти (няма безкрайно влагане), не пише в паметта и
# в списъка със задачи на главния разговор и не пита оператора (докладва).
_NEVER = frozenset({"AGENT", "EXPLORE", "DELEGATE", "REMEMBER", "TASK_ADD", "TASK_UPDATE",
                    "TODO_WRITE", "ASK_USER"})

# Как се вижда напредъкът (чатът го насочва към конзолата си).
progress: Callable[[str], None] | None = None


@dataclass
class Agent:
    name: str
    description: str
    prompt: str
    tools: frozenset[str] | None     # None = всички без _NEVER
    model: str = ""
    source: Path = field(default_factory=Path)
    dropped: tuple[str, ...] = ()    # непознати инструменти от файла


def agent_dirs(workspace: Path) -> list[Path]:
    """Проектът преди оператора — по-близкото печели при едно и също име."""
    from genesis_agent.paths import GENESIS_HOME
    from genesis_agent.project_instructions import _project_root
    try:
        ws = Path(workspace).expanduser().resolve()
    except OSError:
        ws = Path(workspace)
    root = _project_root(ws)
    dirs = [ws / ".genesis" / "agents", ws / ".claude" / "agents"]
    if root != ws:
        dirs += [root / ".genesis" / "agents", root / ".claude" / "agents"]
    return [*dirs, Path(GENESIS_HOME) / "agents"]


def _known_tools() -> set[str]:
    from genesis_agent.tool_schemas import FULL_TOOLS
    return {t["function"]["name"] for t in FULL_TOOLS}


def _tools(raw: Any) -> tuple[frozenset[str] | None, tuple[str, ...]]:
    if raw is None or raw == "" or raw == []:
        return None, ()
    items = raw if isinstance(raw, list) else str(raw).replace(";", ",").split(",")
    known = _known_tools()
    picked: set[str] = set()
    dropped: list[str] = []
    for item in items:
        word = str(item).strip()
        if not word:
            continue
        name = _CLAUDE_TOOLS.get(word.lower().replace("_", ""), word.upper())
        if word.startswith("mcp__"):
            name = word
        if (name in known or name.startswith("mcp__")) and name not in _NEVER:
            picked.add(name)
        else:
            dropped.append(word)
    return frozenset(picked), tuple(dropped)


def _parse(path: Path) -> Agent | None:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")[:_MAX_PROMPT + 4000]
    except OSError:
        return None
    meta: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            import yaml
            try:
                loaded = yaml.safe_load(text[3:end])
            except yaml.YAMLError:
                loaded = None
            meta = loaded if isinstance(loaded, dict) else {}
            body = text[end + 4:]
    name = str(meta.get("name") or path.stem).strip().lower()
    if not _NAME.match(name):
        return None
    prompt = body.strip()[:_MAX_PROMPT]
    description = " ".join(str(meta.get("description") or "").split())[:300]
    if not description:
        description = " ".join((prompt.splitlines() or [""])[0].split())[:200]
    if not prompt and not description:
        return None
    tools, dropped = _tools(meta.get("tools"))
    return Agent(name=name, description=description, prompt=prompt, tools=tools,
                 model=str(meta.get("model") or "").strip().lower(), source=path,
                 dropped=dropped)


def load(workspace: Path) -> dict[str, Agent]:
    found: dict[str, Agent] = {}
    for folder in agent_dirs(workspace):
        try:
            files = sorted(folder.glob("*.md")) if folder.is_dir() else []
        except OSError:
            continue
        for f in files:
            agent = _parse(f)
            if agent is not None and agent.name not in found and len(found) < _MAX_AGENTS:
                found[agent.name] = agent
    return found


def prompt_section(workspace: Path) -> str:
    """Кои под-агенти има — за системния промпт (по един ред)."""
    agents = load(workspace)
    if not agents:
        return ""
    lines = ["## Под-агенти (от .genesis/agents/ — описанията са от файловете им)",
             ("Възложи им задача с AGENT {agent, task} или [AGENT: име | задача]. Работят в "
              "свой контекст и връщат само доклада — ползвай ги за това, за което са описани.")]
    lines += [f"- {a.name} — {a.description}" for a in agents.values()]
    return "\n".join(lines)


def schema(workspace: Path) -> dict | None:
    """Native схемата на AGENT с имената като enum — или None, ако няма агенти."""
    agents = load(workspace)
    if not agents:
        return None
    listing = "; ".join(f"{a.name}: {a.description[:120]}" for a in agents.values())
    return {"type": "function", "function": {
        "name": "AGENT",
        "description": ("Delegate a task to a specialised sub-agent. It works in its own "
                        "context with its own tools and returns only its report. "
                        f"Available: {listing}"[:3000]),
        "parameters": {"type": "object", "properties": {
            "agent": {"type": "string", "enum": sorted(agents)},
            "task": {"type": "string", "description": "The full task: the sub-agent sees "
                     "nothing of this conversation"}},
            "required": ["agent", "task"]}}}


def _schemas(agent: Agent) -> list[dict]:
    from genesis_agent import mcp_client
    from genesis_agent.tool_schemas import FULL_TOOLS
    allowed = allowed_tools(agent)
    out = [t for t in FULL_TOOLS if t["function"]["name"] in allowed]
    out += [s for s in mcp_client.schemas() if s["function"]["name"] in allowed
            or agent.tools is None]
    return out


def allowed_tools(agent: Agent) -> frozenset[str]:
    if agent.tools is not None:
        return agent.tools
    from genesis_agent import mcp_client
    return frozenset((_known_tools() - _NEVER) | {t.qualified for t in mcp_client.tools()})


def _clip(text: str) -> str:
    try:
        from genesis_agent.budget import clip_for_context
        return clip_for_context(text)
    except Exception:
        return text[:8000]


def _say(text: str) -> None:
    if progress is not None:
        try:
            progress(text)
        except Exception:
            pass


def run(name: str, task: str, workspace: Path, *,
        complete: Callable[..., Any] | None = None, max_rounds: int = MAX_ROUNDS) -> str:
    """Изпълнява задачата с под-агента `name` и връща доклада му."""
    import genesis_skills as gs
    name, task = name.strip().lower(), task.strip()
    agents = load(workspace)
    agent = agents.get(name)
    if agent is None:
        have = ", ".join(sorted(agents)) or "няма — създай .genesis/agents/<име>.md"
        return f"[AGENT] ❌ Няма под-агент „{name}“. Има: {have}"
    if not task:
        return f"[AGENT: {name}] ❌ Няма задача."
    if gs._agent_scope() is not None:
        return "[AGENT] ❌ Под-агент не може да вика под-агент."
    if complete is None:
        from genesis_agent.brain import Brain
        complete = Brain(light=agent.model in ("light", "haiku")).complete
    system = (f"{agent.prompt}\n\n---\nТи си под-агент „{agent.name}“ и работиш за друг агент. "
              "Той не вижда нищо от този разговор освен последния ти отговор: свърши задачата, "
              "после отговори с кратък и пълен доклад (какво намери/направи, с път:ред). "
              "Без въпроси към него — ако нещо липсва, кажи го в доклада.")
    try:
        from genesis_agent.agent_core import env_facts
        system += "\n\n" + env_facts(str(workspace))
    except Exception:
        pass
    messages: list[dict] = [{"role": "system", "content": system},
                            {"role": "user", "content": task}]
    allowed = allowed_tools(agent)
    # Прочетеното от под-агента не е „видяно“ от главния: иначе след него
    # WRITE_FILE на главния презаписва файл, който той не е чел (както EXPLORE).
    seen_before = set(gs._SEEN_PATHS)
    gs._set_agent_scope((agent.name, allowed))
    _say(f"↳ {agent.name}: {task[:100]}")
    try:
        return _loop(agent, messages, complete, max_rounds, gs)
    finally:
        gs._set_agent_scope(None)
        gs._SEEN_PATHS.clear()
        gs._SEEN_PATHS.update(seen_before)


def _loop(agent: Agent, messages: list[dict], complete: Callable[..., Any],
          max_rounds: int, gs: Any) -> str:
    schemas = _schemas(agent)
    used = 0
    last = ""
    for round_i in range(max_rounds):
        final = round_i == max_rounds - 1
        if final:  # последният рунд — без инструменти: доклад с каквото е свършено
            messages.append({"role": "user", "content":
                             "Рундовете свършиха. Докладвай сега какво свърши и какво остана."})
        reply = complete(messages, tools=None if final or not schemas else schemas)
        text = (getattr(reply, "raw_text", "") or "").strip()
        if text.startswith("Error:"):
            return f"[AGENT: {agent.name}] ❌ {text[:300]}"
        last = text or last
        calls = [] if final else (getattr(reply, "tool_calls", None) or [])
        if calls:
            calls = calls[:_MAX_CALLS]
            messages.append({"role": "assistant", "content": text, "tool_calls": calls})
            for tc in calls:
                fn = tc.get("function") or {}
                tool = str(fn.get("name") or "")
                _say(f"  ↳ {agent.name}: {tool}")
                result = gs.dispatch_tool_call(tool, fn.get("arguments") or {})
                used += 1
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "name": tool, "content": _clip(result)})
            continue
        outs = [] if final else gs.parse_and_execute_tools(text)
        if outs:
            used += len(outs)
            _say(f"  ↳ {agent.name}: {len(outs)} инструмента")
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": "[резултати]\n" +
                             "\n\n".join(_clip(o) for o in outs[:_MAX_CALLS])})
            continue
        if not text:
            break
        return f"[AGENT: {agent.name}] ({used} инструмента)\n{text[:_MAX_ANSWER]}"
    return (f"[AGENT: {agent.name}] спря след {max_rounds} рунда ({used} инструмента) без "
            f"окончателен доклад. Последно:\n{last[:_MAX_ANSWER]}")


def summary(workspace: Path) -> str:
    """За /agents."""
    agents = load(workspace)
    if not agents:
        dirs = "\n".join(f"  {d}" for d in agent_dirs(workspace)[:2] + agent_dirs(workspace)[-1:])
        return ("Няма под-агенти. Създай файл <име>.md в:\n" + dirs +
                "\n(---\\nname: …\\ndescription: …\\ntools: READ_FILE, SEARCH_CODE\\n---\\nроля)")
    lines = []
    for a in agents.values():
        tools = "всички" if a.tools is None else ", ".join(sorted(a.tools)) or "няма"
        lines.append(f"{a.name} — {a.description}\n  инструменти: {tools}"
                     + (f"\n  ⚠ непознати (пропуснати): {', '.join(a.dropped)}" if a.dropped else "")
                     + f"\n  {a.source}")
    return "\n".join(lines)
