"""
genesis_agent.knowledge_graph — GraphRAG-style structured memory, additive
alongside workspace_memory.py's auto_capture()/briefing(), not a replacement.

Why a separate module: workspace_memory.py's threads/decisions/preferences
schema works and is called from real hook points (agent_core.run_tool_loop's
compaction, the GUI's on_close) — risking that working mechanism for a
rewrite isn't worth it. This adds a second, complementary extraction: not
"what's the next step", but "what IS related to what, and what state is it
in" — entities/relations/active-states, the same shape GraphRAG-style tools
use, small enough here to be one JSON file rather than a graph database.

Fail-open, same convention as workspace_memory.py/provider_stats.py: a
malformed LLM response or a missing file never raises — worst case, the
graph just doesn't grow this round.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from genesis_agent.config import MEMORY_DIR

GRAPH_PATH = MEMORY_DIR / "knowledge_graph.json"

_MAX_RELATIONS_STORED = 200
_MAX_ENTITIES_STORED = 400
_MAX_STATES_STORED = 100
_MAX_LOG_CHARS = 6000

_EXTRACT_PROMPT = (
    "Извлечи от разговора структуриран граф на знанието. Само реални, "
    "конкретни факти — не измисляй връзки, които не са изрично казани.\n\n"
    "Върни САМО валиден JSON, точно тази схема (без markdown fence):\n"
    '{"entities":[{"name":"...","type":"..."}],'
    '"relations":[{"source":"...","relation":"ГЛАГОЛ_UPPERCASE","target":"..."}],'
    '"states":[{"entity":"...","status":"..."}]}\n\n'
    "Примери за relation: REQUIRES, BLOCKS, USES, OWNS, DEPENDS_ON.\n"
    "Ако разговорът няма ясни entity/relation/status факти, върни празни списъци.\n\n"
    "Разговор:\n__TRANSCRIPT__"
)
# .replace(), НЕ .format() — промптът съдържа буквални JSON `{...}` скоби в
# примерната схема; .format(transcript=...) ги чете като placeholder-и и
# гърми с KeyError на всяко извикване (хванат на живо, 2026-07-29 — fail-open
# try/except-ът го гълташе тихо, графът никога не растеше, без грешка никъде).


def _dedup_key(name: str) -> str:
    """Normalizes an entity name for dedup lookup. Plain .lower() only
    catches case differences — an LLM re-mentioning "AuthToken" (code
    symbol) as "auth token" (prose) a few turns later is the MORE common
    case in practice and .lower() alone does NOT merge those (verified live,
    2026-07-29: they landed as two separate entities). Stripping everything
    but letters and digits normalizes both spacing and case in one pass —
    Unicode letters, not [a-z0-9]: that deleted all Cyrillic, every Bulgarian
    name got the key "" and "Базата данни: счупена" was silently overwritten
    by the next state (audit 2026-10-07)."""
    key = re.sub(r"[\W_]+", "", name.casefold())
    return key or name.casefold().strip()


def _empty_graph() -> dict[str, Any]:
    return {"entities": {}, "relations": [], "states": {}}


class _Corrupt(ValueError):
    pass


def _load() -> dict[str, Any]:
    """The graph; empty when there is none. A file that exists but does not
    parse raises _Corrupt — returning an empty graph there made the next merge
    save it over the real one (audit 2026-10-07: 100 entities → 1)."""
    try:
        text = GRAPH_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_graph()
    try:
        data = json.loads(text)
    except ValueError as e:
        raise _Corrupt(str(e)) from e
    if not isinstance(data, dict):
        raise _Corrupt("not an object")
    for key, kind in (("entities", dict), ("relations", list), ("states", dict)):
        if not isinstance(data.get(key), kind):
            data[key] = kind()
    return data


def _set_aside() -> None:
    """Keep an unreadable graph as knowledge_graph.json.corrupt[.N]."""
    for n in range(1, 100):
        kept = GRAPH_PATH.with_name(GRAPH_PATH.name + (".corrupt" if n == 1 else f".corrupt.{n}"))
        if not kept.exists():
            GRAPH_PATH.replace(kept)
            return


def _save(graph: dict[str, Any]) -> None:
    """Atomic: a temp file, then os.replace — a reader never sees half a file."""
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GRAPH_PATH.with_name(f"{GRAPH_PATH.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, GRAPH_PATH)


def _parse_llm_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json")
    result: dict[str, Any] = json.loads(text.strip())
    return result


def _items(extracted: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Only the dict items of a list section. Models answer `"entities":
    ["Redis"]`, `"states": {"Auth": "broken"}`, `"relations": "none"` — each
    was an AttributeError outside the guard (audit 2026-10-07)."""
    section = extracted.get(key)
    return [x for x in section if isinstance(x, dict)] if isinstance(section, list) else []


def compact_and_graph_memory(session_logs: str, workspace: str | None = None) -> dict[str, Any]:
    """LLM extraction pass over `session_logs` (a plain transcript string),
    merged into the persisted graph. Entity dedup is case-insensitive by
    name — the same lesson workspace_memory.add_thread already learned
    (2a87e9b's slug-collision bug): an LLM will re-mention "AuthToken" and
    "auth token" as if new every time otherwise, silently doubling entries
    instead of updating one. Returns the full merged graph.

    `workspace` tags the relations and states, so graph_briefing for one
    project does not show another project's "database: broken"."""
    try:
        graph = _load()
    except _Corrupt:
        graph = _empty_graph()
    if not session_logs.strip():
        return graph

    try:
        from genesis_agent.brain import Brain

        prompt = _EXTRACT_PROMPT.replace(
            "__TRANSCRIPT__", session_logs[-_MAX_LOG_CHARS:]
        )
        reply = Brain(min_size_b=120, light=True).complete([{"role": "user", "content": prompt}])
        extracted = _parse_llm_json(reply.raw_text)
        # Валиден JSON със СГРЕШЕНА форма минаваше през except-а по-долу,
        # защото парсването е успяло — и after that `extracted.get(...)` гърми
        # с AttributeError ИЗВЪН try блока (bug fix, 2026-08-12). Моделът,
        # помолен за обект, редовно връща списък на най-горно ниво; docstring-ът
        # на модула обещава, че точно "malformed LLM response" никога не хвърля.
        if not isinstance(extracted, dict):
            raise TypeError(f"очакван обект, получен {type(extracted).__name__}")
    except Exception:
        return graph  # fail-open — this round just doesn't grow the graph

    # Заредено отново под заключване: обръщението към модела е бавно, а друг
    # процес може да е записал междувременно (одит 2026-10-07: два процеса ×
    # 300 обекта → 80 оцелели).
    from genesis_agent.file_lock import locked
    with locked(GRAPH_PATH):
        try:
            graph = _load()
        except _Corrupt:
            try:
                _set_aside()
            except OSError:
                return graph
            graph = _empty_graph()
        _merge(graph, extracted, workspace)
        # Записът също е под guard: пълен диск или права — а извлеченият граф
        # вече е в паметта и връщането му е по-полезно от изключение от
        # функция, обещала че не хвърля.
        try:
            _save(graph)
        except OSError:
            pass
    return graph


def _merge(graph: dict[str, Any], extracted: dict[str, Any], workspace: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ws = {"ws": workspace} if workspace else {}

    for e in _items(extracted, "entities"):
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        key = _dedup_key(name)
        existing = graph["entities"].get(key, {})
        graph["entities"][key] = {
            "name": name,
            "type": str(e.get("type") or existing.get("type", "")),
            "first_seen": existing.get("first_seen", now),
            "last_seen": now,
        }

    for r in _items(extracted, "relations"):
        source = str(r.get("source") or "").strip()
        target = str(r.get("target") or "").strip()
        relation = str(r.get("relation") or "").strip()
        if source and target and relation:
            graph["relations"].append(
                {"source": source, "relation": relation, "target": target, "at": now, **ws}
            )
    graph["relations"] = graph["relations"][-_MAX_RELATIONS_STORED:]

    for st in _items(extracted, "states"):
        entity = str(st.get("entity") or "").strip()
        status = str(st.get("status") or "").strip()
        if entity and status:
            key = _dedup_key(entity) + (f"@{workspace}" if workspace else "")
            graph["states"][key] = {"name": entity, "status": status, "updated_at": now, **ws}

    # Таван и на обектите и състоянията, не само на връзките: след 500
    # компресии briefing-ът беше 27K знака в системния промпт.
    graph["entities"] = _newest(graph["entities"], "last_seen", _MAX_ENTITIES_STORED)
    graph["states"] = _newest(graph["states"], "updated_at", _MAX_STATES_STORED)


def _newest(items: dict[str, Any], stamp: str, limit: int) -> dict[str, Any]:
    if len(items) <= limit:
        return items
    keep = sorted(items.items(), key=lambda kv: str(kv[1].get(stamp, "")), reverse=True)[:limit]
    return dict(keep)


def graph_briefing(max_relations: int = 12, max_states: int = 12,
                   workspace: str | None = None) -> str:
    """Compact text for system-prompt injection, same role/size budget as
    workspace_memory.briefing() (~150-250 tokens, not a JSON dump). Call
    alongside briefing() when building the startup context. With `workspace`,
    only that project's facts (and untagged ones from before tagging)."""
    try:
        graph = _load()
    except _Corrupt:
        return ""

    def here(item: dict[str, Any]) -> bool:
        return not workspace or item.get("ws") in (None, workspace)

    relations = [r for r in graph["relations"] if isinstance(r, dict) and here(r)]
    lines = [
        f"- {r.get('source')} {r.get('relation')} {r.get('target')}"
        for r in relations[-max_relations:]
    ]
    states = [st for st in graph["states"].values()
              if isinstance(st, dict) and st.get("status") and here(st)]
    states.sort(key=lambda st: str(st.get("updated_at", "")), reverse=True)
    lines += [f"- {st.get('name')}: {st['status']}" for st in states[:max_states]]
    return "\n".join(lines)
