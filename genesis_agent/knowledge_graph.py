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

import contextlib
import json
import logging
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from genesis_agent.config import MEMORY_DIR
from genesis_agent.workspace_memory import STALE_DAYS, mask_secrets

log = logging.getLogger("genesis.knowledge_graph")

GRAPH_PATH = MEMORY_DIR / "knowledge_graph.json"

_MAX_RELATIONS_STORED = 200
_MAX_ENTITIES_STORED = 400
_MAX_STATES_STORED = 100
_MAX_LOG_CHARS = 6000
# Одит 2026-10-31: таван имаше само броят редове (12+12), не дължината им —
# статус с поставен traceback (4 кръга × 3 по 3174 знака) правеше брифинг от
# 35 297 знака (~8.8K токена) в системния промпт на всяка сесия, а нищо по-късно
# не го реже. Затова таван на поле при запис и при четене и знаков бюджет на
# всяка от двете секции на брифинга (същата идея като _PREFS_CHARS в
# workspace_memory).
_MAX_FIELD_CHARS = 200
_BRIEFING_SECTION_CHARS = 1000
# Текстовите полета на всеки вид запис; само те се чистят (_clean_field) —
# "ws" е пътят на проекта и трябва да съвпада буквално, датите са наши.
_TEXT_FIELDS = {
    "entities": ("name", "type"),
    "relations": ("source", "relation", "target"),
    "states": ("name", "status"),
}

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
    by the next state (audit 2026-10-07).

    Одит 2026-10-31: махането на всичко \\W махаше и знаците, които различават
    имената — „C++ модул: мигриран“ се презаписваше от „C# модул: счупен“ в
    същия отговор, C++/C#/C ставаха един обект „c“, „API v1.2“ — „API v12“.
    Затова + → p, # → sharp и точка между цифри → d преди махането. NFKC
    първо: NFD „Йордан“ (имена на файлове от macOS, поставен текст) губеше
    комбиниращата бреве и получаваше ключ „иордан“ — дубликат, който изглежда
    еднакво.

    Преглед 2026-11-05: # → sharp само след буква и не пред цифра (C#, F#) —
    иначе „Issue #123“ и „Issue 123“, „PR #42“ и „PR 42“ спряха да се сливат и
    две противоречащи си състояния на един PR влизаха заедно в брифинга."""
    text = unicodedata.normalize("NFKC", name).casefold()
    text = re.sub(r"(?<=[^\W\d_])#(?!\d)", "sharp", text.replace("+", "p"))
    text = re.sub(r"(?<=\d)\.(?=\d)", "d", text)
    key = re.sub(r"[\W_]+", "", text)
    return key or name.casefold().strip()


def _clean_field(value: Any) -> str:
    """Един ред, до _MAX_FIELD_CHARS знака, без тайни и без самотни surrogates.

    Одит 2026-10-31, възпроизведено през Core(): (1) `postgres://admin:парола@…`,
    `sk_live_…`, `sk-proj-…` от разговора отиваха дословно във файла и оттам в
    системния промпт на всяка следваща сесия в папката, пратен до който доставчик
    обслужва хода — същият теч, който workspace_memory затвори на 2026-10-22,
    затова и същото mask_secrets. (2) Статус „прочетен\\n\\n## ИНСТРУКЦИИ НА
    ОПЕРАТОРА …“ ставаше отделно заглавие в системния промпт — текст, преписан от
    поставен документ, получаваше ранга на системна инструкция; „Redis\\r\\nкеш“
    чупеше реда „- a REL b“. (3) Неспарен „\\ud83d“ от модела гърмеше в записа с
    UnicodeEncodeError и губеше целия кръг. Маскира се ПРЕДИ рязането: отрязан
    наполовина ключ вече не отговаря на шаблона и би изтекъл частично."""
    text = str(value or "").encode("utf-8", "replace").decode("utf-8")
    return mask_secrets(" ".join(text.split()))[:_MAX_FIELD_CHARS]


def _empty_graph() -> dict[str, Any]:
    return {"entities": {}, "relations": [], "states": {}}


class _Corrupt(ValueError):
    pass


def _load() -> dict[str, Any]:
    """The graph; empty when there is none. A file that exists but does not
    parse raises _Corrupt — returning an empty graph there made the next merge
    save it over the real one (audit 2026-10-07: 100 entities → 1). Друг
    OSError (права, папка на мястото на файла) се вдига — извикващите са
    fail-open."""
    try:
        raw = GRAPH_PATH.read_bytes()
    except FileNotFoundError:
        return _empty_graph()
    # Декодиране вътре в try-а и с utf-8-sig (одит 2026-10-31): read_text беше
    # извън него и файл, който не е UTF-8 (ръчна редакция в cp1251 — PowerShell
    # 5.1 Set-Content пише ANSI; копие, прекъснато по средата на кирилска буква),
    # вдигаше UnicodeDecodeError от брифинга и от компресията при всяко
    # извикване, без да бъде отделен като .corrupt. А валиден граф с BOM
    # (Set-Content -Encoding UTF8, стар Notepad) се смяташе за повреден и
    # следващото сливане го заменяше с празен. UnicodeDecodeError е ValueError.
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except ValueError as e:
        raise _Corrupt(str(e)) from e
    if not isinstance(data, dict):
        raise _Corrupt("not an object")
    for key, kind in (("entities", dict), ("relations", list), ("states", dict)):
        if not isinstance(data.get(key), kind):
            data[key] = kind()
    return _sanitized(data)


def _sanitized(data: dict[str, Any]) -> dict[str, Any]:
    """Графът от диска, подреден така, както _merge би го записал днес.

    Одит 2026-10-31: (1) проверяваха се типовете на секциите, не на записите —
    ръчно „поправено“ състояние `"бд": "оправено"` (няма команда за редакция)
    гърмеше с AttributeError в _newest на всяка компресия, а null обект — щом
    моделът го спомене; изключението излизаше изпод заключването, agent_core го
    гълташе и графът спираше да расте, докато брифингът си работеше. Записи,
    които не са обекти или нямат текст, отпадат. (2) Графите отпреди поправките
    носят тайни, многоредови/огромни полета и повторени връзки — изчистват се
    тук, така че и брифингът, и следващият запис ги виждат чисти. (3) Ключовете
    се смятат наново: иначе „C++ модул“ под стария ключ „cмодул“ и новото му
    споменаване под „cppмодул“ щяха да се показват като два факта."""
    graph = _empty_graph()
    for section, stamp in (("entities", "last_seen"), ("states", "updated_at")):
        for item in data[section].values():
            if not isinstance(item, dict):
                continue
            item = _cleaned(item, section)
            if not item["name"] or (section == "states" and not item["status"]):
                continue
            key = _dedup_key(item["name"])
            if section == "states" and item.get("ws"):
                key += f"@{item['ws']}"
            kept = graph[section].get(key)
            if kept is None or str(item.get(stamp, "")) >= str(kept.get(stamp, "")):
                graph[section][key] = item
    graph["relations"] = _unique_relations(
        [_cleaned(r, "relations") for r in data["relations"] if isinstance(r, dict)])
    return graph


def _cleaned(item: dict[str, Any], section: str) -> dict[str, Any]:
    return {**item, **{f: _clean_field(item.get(f)) for f in _TEXT_FIELDS[section]}}


def _unique_relations(relations: list[dict[str, Any]],
                      per_workspace: bool = True) -> list[dict[str, Any]]:
    """Една връзка на (source, relation, target, ws); последното споменаване
    печели и отива най-отзад. Непълните отпадат.

    Одит 2026-10-31, през истинския run_tool_loop: 10-те съобщения, които
    компресията пази, влизат пак в pre_compact на следващата и фактите им се
    извличат отново — 6 извличания дадоха 13 връзки, 6 различни, а брифингът
    (последните 12) показваше същите факти отново и отново и избутваше
    различните; таванът от 200 гонеше истински по-стари връзки.

    Преглед 2026-11-05: str(ws) в ключа — ръчно редактирано `"ws": ["/x"]`
    правеше ключа нехешируем и TypeError спираше и брифинга, и всяка
    компресия, без файлът да бъде отделен (същият клас като записите, които не
    са обекти)."""
    unique: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for r in relations:
        if r["source"] and r["relation"] and r["target"]:
            ws = str(r.get("ws") or "") if per_workspace else ""
            key = (_dedup_key(r["source"]), _dedup_key(r["relation"]),
                   _dedup_key(r["target"]), ws)
            unique.pop(key, None)
            unique[key] = r
    return list(unique.values())


def _set_aside() -> None:
    """Keep an unreadable graph as knowledge_graph.json.corrupt[.N]."""
    for n in range(1, 100):
        kept = GRAPH_PATH.with_name(GRAPH_PATH.name + (".corrupt" if n == 1 else f".corrupt.{n}"))
        if not kept.exists():
            GRAPH_PATH.replace(kept)
            return


def _save(graph: dict[str, Any]) -> None:
    """Atomic: a temp file, then os.replace — a reader never sees half a file.

    Одит 2026-10-31: при провален запис (пълен диск, заключен файл, самотен
    surrogate) knowledge_graph.json.<pid>.tmp — пълно копие на графа — оставаше
    завинаги до файла, в git checkout и непокрит от .gitignore. „replace“ при
    кодирането: surrogate може да дойде и извън полетата, които _clean_field
    чисти (напр. пътят на проекта)."""
    GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GRAPH_PATH.with_name(f"{GRAPH_PATH.name}.{os.getpid()}.tmp")
    data = json.dumps(graph, ensure_ascii=False, indent=2).encode("utf-8", "replace")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, GRAPH_PATH)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


# Одит 2026-10-31: „```JSON“, изречение преди обекта, бележка след оградата и
# „<think>…</think>“ отпред от разсъждаващ модел падаха на json.loads и кръгът
# се губеше тихо, докато workspace_memory.auto_capture приемаше същия отговор.
# Преглед 2026-11-05: всичко до ПОСЛЕДНОТО </think>, не двойката <think>…</think> —
# доставчик, който изпуска отварящия таг, връща „разсъждение…</think>{json}“, а
# extract_json_object тогава вземаше първия обект от разсъжденията (повторената
# схема „{"entities":[]}“) и кръгът излизаше празен.
_THINK_RE = re.compile(r"(?is)^.*</think>")


def _parse_llm_json(raw: str) -> dict[str, Any]:
    text = _THINK_RE.sub("", raw, count=1).strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json")
    result: dict[str, Any]
    try:
        result = json.loads(text.strip())
    except ValueError:
        from genesis_agent.llm_api import extract_json_object

        found = extract_json_object(text)
        if found is None:
            raise  # няма обект — fail-open горе, както досега
        result = json.loads(found)
    return result


def _items(extracted: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Only the dict items of a list section. Models answer `"entities":
    ["Redis"]`, `"states": {"Auth": "broken"}`, `"relations": "none"` — each
    was an AttributeError outside the guard (audit 2026-10-07)."""
    section = extracted.get(key)
    return [x for x in section if isinstance(x, dict)] if isinstance(section, list) else []


def _log_window(session_logs: str) -> str:
    """Началото и краят на разговора, не само краят.

    Одит 2026-10-31, през истинския run_tool_loop: agent_core подава цялата
    история преди компресията, а в чат за код последните ~10 съобщения (тези,
    които компресията ПАЗИ) са код по ~900-1365 знака и запълваха всичките
    6000 знака. По-старите — които компресията заменя с обобщение и в които
    са решенията („плащанията минават през Stripe, базата е PostgreSQL 16“) —
    не стигаха до модела в нито едно от 6-те извличания. Само предпазител за
    готов низ: agent_core подава transcript(), който вече се побира."""
    return _clip_middle(session_logs, _MAX_LOG_CHARS, "\n…\n")


def transcript(messages: Iterable[Mapping[str, Any]]) -> str:
    """Преписът за compact_and_graph_memory: всяко user/assistant съобщение
    получава дял от _MAX_LOG_CHARS, така че никое не изпада изцяло.

    Преглед 2026-11-05 (шести кръг на одита), през истинския run_tool_loop
    с компресия „обобщение + последните 10“, 30 хода и факт във всяко user
    съобщение: _log_window сам не стига, щом отговорите са дълги — 3000 знака
    начало събират едно-две съобщения, а средата изпада. От фактите, които
    никога не стигнаха до графа: при отговори ~1.2K знака 2/29, ~1.9K — 12/29,
    ~3.8K и ~10K — 21/29. Дяловете са max-min: късото съобщение влиза цяло
    (решенията обикновено са кратки реплики), дългите делят остатъка поравно и
    пазят начало и край. Със същото измерване — 0/29 при всички размери.
    Фиксиран таван от 600 знака на съобщение също дава 0/29 там, но при 8
    рунда с инструменти на ход (~3.8K знака на отговор) губи 19/20: 15+ дълги
    съобщения пак надхвърлят бюджета и средата изпада заедно с късата user
    реплика в нея; с дяловете — 0/20."""
    lines = [f"{m.get('role')}: {m.get('content')}" for m in messages
             if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    caps = _fair_shares([len(line) for line in lines], max(0, _MAX_LOG_CHARS - len(lines) + 1))
    return "\n".join(_clip_middle(line, cap) for line, cap in zip(lines, caps))


def _fair_shares(lengths: list[int], budget: int) -> list[int]:
    """Max-min разпределение на `budget`: от най-късото нагоре всяко взима
    колкото му трябва, но не повече от равен дял от остатъка."""
    shares = [0] * len(lengths)
    left = budget
    order = sorted(range(len(lengths)), key=lengths.__getitem__)
    for done, i in enumerate(order):
        shares[i] = min(lengths[i], left // (len(order) - done))
        left -= shares[i]
    return shares


def _clip_middle(text: str, limit: int, cut: str = " … ") -> str:
    """Началото и краят на `text`, общо до `limit` знака."""
    if len(text) <= limit:
        return text
    room = limit - len(cut)
    if room < 2:
        return text[:max(0, limit)]
    head = (room + 1) // 2
    return text[:head] + cut + text[-(room - head):]


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
    except (_Corrupt, OSError):
        graph = _empty_graph()
    if not session_logs.strip():
        return graph

    try:
        from genesis_agent.brain import Brain

        prompt = _EXTRACT_PROMPT.replace("__TRANSCRIPT__", _log_window(session_logs))
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
        except OSError:
            return graph  # нечетим (права, папка на мястото му) — не пишем върху него
        _merge(graph, extracted, workspace)
        # Записът също е под guard: пълен диск или права — а извлеченият граф
        # вече е в паметта и връщането му е по-полезно от изключение от
        # функция, обещала че не хвърля.
        try:
            _save(graph)
        except (OSError, ValueError):
            log.debug("графът не се записа", exc_info=True)
    return graph


def _merge(graph: dict[str, Any], extracted: dict[str, Any], workspace: str | None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ws = {"ws": workspace} if workspace else {}

    for e in _items(extracted, "entities"):
        name = _clean_field(e.get("name"))
        if not name:
            continue
        key = _dedup_key(name)
        existing = graph["entities"].get(key, {})
        graph["entities"][key] = {
            "name": name,
            "type": _clean_field(e.get("type")) or existing.get("type", ""),
            "first_seen": existing.get("first_seen", now),
            "last_seen": now,
        }

    fresh = [
        {"source": _clean_field(r.get("source")), "relation": _clean_field(r.get("relation")),
         "target": _clean_field(r.get("target")), "at": now, **ws}
        for r in _items(extracted, "relations")
    ]
    graph["relations"] = _unique_relations(graph["relations"] + fresh)[-_MAX_RELATIONS_STORED:]

    for st in _items(extracted, "states"):
        entity = _clean_field(st.get("entity"))
        status = _clean_field(st.get("status"))
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
    only that project's facts (and untagged ones from before tagging).
    Един факт — един ред (полетата вече са изчистени от _load), всяка от двете
    секции е до _BRIEFING_SECTION_CHARS знака."""
    try:
        graph = _load()
    except (_Corrupt, OSError):
        return ""

    def here(item: dict[str, Any]) -> bool:
        return not workspace or item.get("ws") in (None, workspace)

    # Преглед 2026-11-05: дедупликация и СЛЕД филтъра — връзка/състояние без
    # ws отпреди маркирането и същият факт, извлечен наново с ws="/a", са два
    # записа, а брифингът за /a печаташе „- API USES Redis“ два пъти и старото
    # „БД: счупена“ до новото „БД: оправена“. По-новото печели. Без workspace
    # проектите остават отделни — там ws различава факти, а не копия.
    relations = _unique_relations([r for r in graph["relations"] if here(r)],
                                  per_workspace=not workspace)[-max_relations:]
    rel_lines = [f"- {r['source']} {r['relation']} {r['target']}{_age(r.get('at'))}"
                 for r in reversed(relations)]
    newest: dict[str, dict[str, Any]] = {}
    for st in sorted((st for st in graph["states"].values() if here(st)),
                     key=lambda st: str(st.get("updated_at", "")), reverse=True):
        ws = "" if workspace else f"@{st.get('ws') or ''}"
        newest.setdefault(_dedup_key(st["name"]) + ws, st)
    states = list(newest.values())
    state_lines = [f"- {st['name']}: {st['status']}{_age(st.get('updated_at'))}"
                   for st in states[:max_states]]
    # Най-новите влизат първи в бюджета; връзките се показват в реда на запис.
    return "\n".join(_fit(rel_lines)[::-1] + _fit(state_lines))


def _fit(lines: list[str]) -> list[str]:
    out: list[str] = []
    used = 0
    for line in lines:
        if out and used + len(line) > _BRIEFING_SECTION_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    return out


def _age(stamp: Any) -> str:
    """„ (преди N дни)“ за факт, по-стар от workspace_memory.STALE_DAYS.

    Одит 2026-10-31: нищо не остарява и не се трие, а брифингът не казваше
    възраст — „Деплой: блокиран до ревю“ отпреди 244 дни се подаваше като
    текущо състояние до по-новото „БД: оправена“. Същата граница като
    заспалите нишки на workspace_memory (остарял списък е по-лош от никакъв)."""
    try:
        then = datetime.fromisoformat(str(stamp))
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - then).days
    return f" (преди {days} дни)" if days >= STALE_DAYS else ""
