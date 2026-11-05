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
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

from genesis_agent.config import MEMORY_DIR
from genesis_agent.workspace_memory import mask_secrets

log = logging.getLogger("genesis.knowledge_graph")

GRAPH_PATH = MEMORY_DIR / "knowledge_graph.json"

_MAX_RELATIONS_STORED = 200
_MAX_ENTITIES_STORED = 400
_MAX_STATES_STORED = 100
_MAX_LOG_CHARS = 6000
# Таван на ВСЯКО поле, не само на броя редове (одит 2026-11-04): 12 връзки и
# 12 състояния с по 2400 знака „status“ от модела дадоха брифинг от 57 887
# знака (~15K токена) в системния промпт на всеки рунд, а файлът — 4 MB.
_MAX_FIELD_CHARS = 160

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
    # "replace": половин сурогат от модела (`"\ud83d"` — отрязано емоджи) става „?“
    # (2026-11-05, отвореното от одита на 04.11): UnicodeEncodeError е ValueError,
    # не OSError — compact_and_graph_memory хвърляше и оставяше `.tmp` до графа.
    tmp.write_bytes(json.dumps(graph, ensure_ascii=False, indent=2).encode("utf-8", "replace"))
    os.replace(tmp, GRAPH_PATH)


_SECTIONS = ("entities", "relations", "states")


def _parse_llm_json(raw: str) -> dict[str, Any]:
    """Обектът с графа от отговора на модела, и когато е в проза или ограда.

    Одит 2026-11-04: само „```json“ в самото начало се приемаше —
    „Ето графа:\\n```json …```“, „```JSON“, ограда с изречение след нея и
    „Ето JSON: {…}“ гърмяха с JSONDecodeError и графът тихо не растеше
    (workspace_memory.auto_capture със същия лек модел търси `{` по същата
    причина). Взима се първият обект с поне една от секциите на схемата —
    не просто първият `{`, за да не се вземе вложен обект от проза."""
    text = raw.strip()
    decoder = json.JSONDecoder()
    for start in [i for i, ch in enumerate(text) if ch == "{"][:200]:
        try:
            obj, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(obj, dict) and any(k in obj for k in _SECTIONS):
            return obj
    result: dict[str, Any] = json.loads(text)
    return result


def _text(value: Any) -> str:
    """Поле от модела, готово за файла и за промпта: без тайни и с таван.

    Одит 2026-11-04, възпроизведено: „ключът за stripe е sk_live_…“ в
    разговора ставаше състояние „Stripe ключ: зададен: sk_live_…“, а
    graph_briefing го слагаше в системния промпт на всяка следваща сесия в
    проекта — REMEMBER маскира от 2026-10-22, графът не. Първо маскиране,
    после рязане: отрязан наполовина ключ вече не прилича на ключ."""
    out = mask_secrets(str(value or "").strip())
    return out if len(out) <= _MAX_FIELD_CHARS else out[:_MAX_FIELD_CHARS - 1] + "…"


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
        log.debug("извличането за графа се провали — графът не расте", exc_info=True)
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
        name = _text(e.get("name"))
        if not name:
            continue
        key = _dedup_key(name)
        existing = graph["entities"].get(key, {})
        graph["entities"][key] = {
            "name": name,
            "type": _text(e.get("type") or existing.get("type", "")),
            "first_seen": existing.get("first_seen", now),
            "last_seen": now,
        }

    # Същата връзка, извлечена пак, замества старата (и става най-нова),
    # вместо да се добави втори път. Одит 2026-11-04: компресията пази
    # последните 10 реплики (agent_core.COMPACT_KEEP_RECENT) и следващата ги
    # подава на модела пак, тоест същите факти — 2 факта × 6 компресии
    # запълниха и 12-те реда на брифинга с повторения, а таванът от 200
    # изтласкваше различните факти. Ключът е като на обектите (AuthToken =
    # auth token) и е по проект; речникът пази реда, pop+запис мести в края.
    relations: dict[tuple[str, ...], dict[str, Any]] = {}
    for old in graph["relations"]:
        if isinstance(old, dict):
            rkey = _relation_key(old.get("source"), old.get("relation"), old.get("target"),
                                 old.get("ws"))
            relations.pop(rkey, None)
            relations[rkey] = old
    for r in _items(extracted, "relations"):
        source, target = _text(r.get("source")), _text(r.get("target"))
        relation = _text(r.get("relation"))
        if source and target and relation:
            rkey = _relation_key(source, relation, target, workspace)
            relations.pop(rkey, None)
            relations[rkey] = {"source": source, "relation": relation, "target": target,
                               "at": now, **ws}
    graph["relations"] = list(relations.values())[-_MAX_RELATIONS_STORED:]

    for st in _items(extracted, "states"):
        entity = _text(st.get("entity"))
        status = _text(st.get("status"))
        if entity and status:
            key = _dedup_key(entity) + (f"@{workspace}" if workspace else "")
            graph["states"][key] = {"name": entity, "status": status, "updated_at": now, **ws}

    # Таван и на обектите и състоянията, не само на връзките: след 500
    # компресии briefing-ът беше 27K знака в системния промпт.
    graph["entities"] = _newest(graph["entities"], "last_seen", _MAX_ENTITIES_STORED)
    graph["states"] = _newest(graph["states"], "updated_at", _MAX_STATES_STORED)


def _relation_key(source: Any, relation: Any, target: Any, workspace: Any) -> tuple[str, ...]:
    return (_dedup_key(str(source or "")), _dedup_key(str(relation or "")),
            _dedup_key(str(target or "")), str(workspace or ""))


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

    # _text и при четене: записите отпреди 2026-11-04 са във файла дословно —
    # с ключовете и с дължината, с която моделът ги е дал.
    relations = [r for r in graph["relations"] if isinstance(r, dict) and here(r)]
    lines = [
        f"- {_text(r.get('source'))} {_text(r.get('relation'))} {_text(r.get('target'))}"
        for r in relations[-max_relations:]
    ]
    states = [st for st in graph["states"].values()
              if isinstance(st, dict) and st.get("status") and here(st)]
    states.sort(key=lambda st: str(st.get("updated_at", "")), reverse=True)
    lines += [f"- {_text(st.get('name'))}: {_text(st['status'])}" for st in states[:max_states]]
    return "\n".join(lines)
