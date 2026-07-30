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
import re
from datetime import datetime, timezone
from typing import Any

from genesis_agent.config import DATA_DIR

GRAPH_PATH = DATA_DIR / "knowledge_graph.json"

_MAX_RELATIONS_STORED = 200
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
    but alphanumerics normalizes both spacing and case in one pass."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _empty_graph() -> dict[str, Any]:
    return {"entities": {}, "relations": [], "states": {}}


def _load() -> dict[str, Any]:
    if not GRAPH_PATH.exists():
        return _empty_graph()
    try:
        data: dict[str, Any] = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
        for key in ("entities", "relations", "states"):
            data.setdefault(key, {} if key != "relations" else [])
        return data
    except (json.JSONDecodeError, OSError):
        return _empty_graph()


def _save(graph: dict[str, Any]) -> None:
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    GRAPH_PATH.write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _parse_llm_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json")
    result: dict[str, Any] = json.loads(text.strip())
    return result


def compact_and_graph_memory(session_logs: str) -> dict[str, Any]:
    """LLM extraction pass over `session_logs` (a plain transcript string),
    merged into the persisted graph. Entity dedup is case-insensitive by
    name — the same lesson workspace_memory.add_thread already learned
    (2a87e9b's slug-collision bug): an LLM will re-mention "AuthToken" and
    "auth token" as if new every time otherwise, silently doubling entries
    instead of updating one. Returns the full merged graph."""
    graph = _load()
    if not session_logs.strip():
        return graph

    try:
        from genesis_agent.brain import Brain

        prompt = _EXTRACT_PROMPT.replace(
            "__TRANSCRIPT__", session_logs[-_MAX_LOG_CHARS:]
        )
        reply = Brain(min_size_b=120).complete([{"role": "user", "content": prompt}])
        extracted = _parse_llm_json(reply.raw_text)
    except Exception:
        return graph  # fail-open — this round just doesn't grow the graph

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for e in extracted.get("entities") or []:
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

    for r in extracted.get("relations") or []:
        source = str(r.get("source") or "").strip()
        target = str(r.get("target") or "").strip()
        relation = str(r.get("relation") or "").strip()
        if source and target and relation:
            graph["relations"].append(
                {"source": source, "relation": relation, "target": target, "at": now}
            )
    graph["relations"] = graph["relations"][-_MAX_RELATIONS_STORED:]

    for s in extracted.get("states") or []:
        entity = str(s.get("entity") or "").strip()
        status = str(s.get("status") or "").strip()
        if entity and status:
            graph["states"][_dedup_key(entity)] = {
                "name": entity, "status": status, "updated_at": now
            }

    _save(graph)
    return graph


def graph_briefing(max_relations: int = 12) -> str:
    """Compact text for system-prompt injection, same role/size budget as
    workspace_memory.briefing() (~150-250 tokens, not a JSON dump). Call
    alongside briefing() when building the startup context."""
    graph = _load()
    lines = [
        f"- {r['source']} {r['relation']} {r['target']}"
        for r in graph["relations"][-max_relations:]
    ]
    lines += [
        f"- {s.get('name', key)}: {s['status']}"
        for key, s in graph["states"].items() if s.get("status")
    ]
    return "\n".join(lines)
