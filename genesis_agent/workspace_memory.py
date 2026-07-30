"""
genesis_agent.workspace_memory — памет за РАБОТАТА, не за командите (design note, 2026-07-25).

Защо съществува: досега "паметта" на Genesis при старт беше последните N
епизода — на практика лог от `RUN_CMD echo ...` извиквания — плюс 4 статични
KV реда. Нищо за това какво реално работи потребителят, какво е решено, какво предстои.
Освен това Genesis нямаше НИТО ЕДИН инструмент за запис в паметта си: можеше
само да чете инжектираното при старт. Резултат: всяка сесия започваше с амнезия
за същината и потребителят трябваше да преразказва контекста наново.

Тук се пази това, което оцелява между сесиите и има значение:

    threads     — отворени нишки работа: заглавие, статус, СЛЕДВАЩА СТЪПКА.
    decisions   — какво е решено и ЗАЩО (за да не се предоговаря наново).
    preferences — научени предпочитания как потребителят иска нещата.

Три отделни таблици в една SQLite база, съзнателно проста схема. Пише се от
самия Genesis през новите tool-ове (REMEMBER / TASK_ADD / TASK_UPDATE), не
само от кода — затова всяка публична функция е защитена срещу боклук вход
(празни низове, невалиден статус, прекалено дълъг текст).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from genesis_agent.config import DATA_DIR

DB_PATH = DATA_DIR / "workspace_memory.db"

STATUSES = ("open", "blocked", "done")
_MAX_TEXT = 2000  # рязък таван — LLM понякога праща цял абзац за "заглавие"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',
    next_step  TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    what       TEXT NOT NULL,
    why        TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    topic      TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(_SCHEMA)
    return conn


def _clean(text: Any, limit: int = _MAX_TEXT) -> str:
    return str(text or "").strip()[:limit]


# ─── Нишки работа ────────────────────────────────────────────────────────────

def add_thread(title: str, next_step: str = "", notes: str = "") -> str:
    """Отваря нова нишка работа. Дедуплицира по заглавие (case-insensitive) —
    LLM-ът лесно би добавил същата задача два пъти в различни сесии."""
    title = _clean(title, 300)
    if not title:
        return "[TASK_ADD] Празно заглавие — нищо не е добавено."
    with _conn() as c:
        row = c.execute(
            "SELECT id, status FROM threads WHERE lower(title) = lower(?);", (title,)
        ).fetchone()
        if row:
            # Съществува — обновяваме следващата стъпка вместо да дублираме.
            c.execute(
                "UPDATE threads SET next_step = ?, updated_at = ? WHERE id = ?;",
                (_clean(next_step) or "", _now(), row[0]),
            )
            return f"[TASK_ADD] Нишка #{row[0]} вече съществува ({row[1]}) — обнових следващата стъпка."
        cur = c.execute(
            "INSERT INTO threads (title, status, next_step, notes, created_at, updated_at)"
            " VALUES (?, 'open', ?, ?, ?, ?);",
            (title, _clean(next_step), _clean(notes), _now(), _now()),
        )
        return f"[TASK_ADD] ✓ Нишка #{cur.lastrowid}: {title}"


def update_thread(thread_id: Any, status: str = "", next_step: str = "", notes: str = "") -> str:
    """Обновява статус/следваща стъпка. Празен аргумент = не пипай това поле."""
    try:
        tid = int(str(thread_id).strip().lstrip("#"))
    except (ValueError, TypeError):
        return f"[TASK_UPDATE] Невалиден id: {thread_id!r} (очаквам число, виж TASK_LIST)."
    status = _clean(status, 20).lower()
    if status and status not in STATUSES:
        return f"[TASK_UPDATE] Невалиден статус {status!r}. Позволени: {', '.join(STATUSES)}."
    sets: list[str] = []
    params: list[Any] = []  # съдържа и tid (int) накрая
    if status:
        sets.append("status = ?"); params.append(status)
    if next_step:
        sets.append("next_step = ?"); params.append(_clean(next_step))
    if notes:
        sets.append("notes = ?"); params.append(_clean(notes))
    if not sets:
        return "[TASK_UPDATE] Нищо за обновяване (подай status, next_step или notes)."
    sets.append("updated_at = ?"); params.append(_now())
    params.append(tid)
    with _conn() as c:
        cur = c.execute(f"UPDATE threads SET {', '.join(sets)} WHERE id = ?;", params)
        if cur.rowcount == 0:
            return f"[TASK_UPDATE] Няма нишка #{tid}."
    return f"[TASK_UPDATE] ✓ Нишка #{tid} обновена."


def list_threads(status: str = "open", limit: int = 20) -> list[dict]:
    q = "SELECT id, title, status, next_step, notes, updated_at FROM threads"
    params: list = []
    if status and status != "all":
        q += " WHERE status = ?"; params.append(status)
    q += " ORDER BY updated_at DESC LIMIT ?;"
    params.append(int(limit))
    with _conn() as c:
        rows = c.execute(q, params).fetchall()
    return [
        {"id": r[0], "title": r[1], "status": r[2],
         "next_step": r[3], "notes": r[4], "updated_at": r[5]}
        for r in rows
    ]


# ─── Решения и предпочитания ─────────────────────────────────────────────────

def _is_semantic_dup(text: str, existing: list[str]) -> str | None:
    """Връща съществуващ запис, ако новият е практически същият текст.

    Съзнателно БЕЗ embedding-базирано сравнение. Измерено на живо с локалния
    `nomic-embed-text` (2026-07-25):
        идентичен текст (само различен регистър) → 0.861
        същият смисъл, друга формулировка       → 0.776
        ПРОТИВОПОЛОЖЕН смисъл, същата тема      → 0.668
          ("комити на български" vs "комити на английски")
        напълно несвързани                      → 0.608
    Разликата между "същото" и "обратното" е ~0.11, а идентичен текст дори не
    стига 0.9. Праг, който хваща перифразите, неизбежно слива и противоположни
    предпочитания — а да мислиш, че потребителят иска английски комити, когато иска
    български, е по-лошо от дубликат. Затова тук само точно съвпадение, а
    смисловото обединяване го върши extraction промптът (вижда съществуващите
    записи и преизползва техните формулировки) — LLM-ът се справя с това,
    embedding прагът — не.
    """
    if not existing:
        return None
    norm = lambda s: "".join(ch.lower() for ch in s if ch.isalnum() or ch.isspace()).strip()
    n_new = norm(text)
    for e in existing:
        if norm(e) == n_new:
            return e
    return None


def add_decision(what: str, why: str = "") -> str:
    what = _clean(what, 500)
    if not what:
        return "[REMEMBER] Празно решение — нищо не е записано."
    # Дедупликация: auto_capture върви и при компресия, и на изход, често върху
    # застъпващо се съдържание — без това едно решение влиза 2-3 пъти с леко
    # различна формулировка и брифингът бързо става шум.
    existing = [d["what"] for d in list_decisions(50)]
    dup = _is_semantic_dup(what, existing)
    if dup:
        return f"[REMEMBER] Вече е записано (като «{dup[:60]}») — пропуснато."
    with _conn() as c:
        c.execute("INSERT INTO decisions (what, why, created_at) VALUES (?, ?, ?);",
                  (what, _clean(why, 500), _now()))
    return f"[REMEMBER] ✓ Записано решение: {what[:80]}"


def set_preference(topic: str, value: str) -> str:
    topic, value = _clean(topic, 100), _clean(value, 500)
    if not topic or not value:
        return "[REMEMBER] Нужни са и тема, и стойност."
    # Темата е PRIMARY KEY, така че същата тема се презаписва сама. Но същото
    # предпочитание често идва под РАЗЛИЧНА тема ("стил на комуникация" vs
    # "обяснения") — затова сравняваме и по стойност.
    existing = list_preferences()
    if topic not in existing:
        dup_val = _is_semantic_dup(value, list(existing.values()))
        if dup_val:
            old_topic = next((t for t, v in existing.items() if v == dup_val), None)
            if old_topic:
                # Обновяваме съществуващия запис вместо да добавяме синоним.
                with _conn() as c:
                    c.execute("UPDATE preferences SET value = ?, updated_at = ? WHERE topic = ?;",
                              (value, _now(), old_topic))
                return f"[REMEMBER] Обединено със съществуващото «{old_topic}»."
    with _conn() as c:
        c.execute(
            "INSERT INTO preferences (topic, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(topic) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at;",
            (topic, value, _now()),
        )
    return f"[REMEMBER] ✓ Предпочитание «{topic}»: {value[:80]}"


def list_decisions(limit: int = 10) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT what, why, created_at FROM decisions ORDER BY id DESC LIMIT ?;",
            (int(limit),),
        ).fetchall()
    return [{"what": r[0], "why": r[1], "created_at": r[2]} for r in rows]


def list_preferences() -> dict[str, str]:
    with _conn() as c:
        rows = c.execute("SELECT topic, value FROM preferences ORDER BY topic;").fetchall()
    return {r[0]: r[1] for r in rows}


# ─── Брифинг (това се инжектира в промпта) ───────────────────────────────────

def briefing(max_threads: int = 8, max_decisions: int = 5) -> str:
    """Компактен, ПОЛЕЗЕН контекст за начало на сесия — отворена работа със
    следващи стъпки, скорошни решения, научени предпочитания. Заменя стария
    дъмп от сурови RUN_CMD ехота, който не носеше никаква информация."""
    parts: list[str] = []

    # Свежите (пипани скоро) горе — те са реалната работа. Остарелите долу,
    # компактно, с покана да се затворят: така списъкът не расте безкрайно и
    # брифингът остава четим дори след седмици употреба.
    all_open = list_threads("open", 100)
    fresh = [t for t in all_open if _days_since(t["updated_at"]) < STALE_DAYS][:max_threads]
    stale = [t for t in all_open if _days_since(t["updated_at"]) >= STALE_DAYS]
    blocked = list_threads("blocked", max_threads)

    if fresh or blocked:
        lines = []
        for t in fresh:
            nxt = f" → СЛЕДВА: {t['next_step']}" if t["next_step"] else ""
            lines.append(f"  #{t['id']} {t['title']}{nxt}")
        for t in blocked:
            nxt = f" → блокирано: {t['next_step']}" if t["next_step"] else ""
            lines.append(f"  #{t['id']} ⛔ {t['title']}{nxt}")
        parts.append("Отворена работа:\n" + "\n".join(lines))

    if stale:
        lines = [f"  #{t['id']} {t['title'][:70]} ({_days_since(t['updated_at'])}д)"
                 for t in stale[:8]]
        extra = f"\n  … и още {len(stale) - 8}" if len(stale) > 8 else ""
        parts.append(f"Заспали нишки (>{STALE_DAYS} дни без движение — питай потребителят дали "
                     f"да се затворят, не ги подхващай сам):\n" + "\n".join(lines) + extra)

    decisions = list_decisions(max_decisions)
    if decisions:
        lines = [f"  • {d['what']}" + (f" (защото: {d['why']})" if d["why"] else "")
                 for d in decisions]
        parts.append("Взети решения (не ги предоговаряй без причина):\n" + "\n".join(lines))

    prefs = list_preferences()
    if prefs:
        lines = [f"  • {k}: {v}" for k, v in prefs.items()]
        parts.append("Предпочитания на потребителя:\n" + "\n".join(lines))

    return "\n\n".join(parts)


_CAPTURE_PROMPT = """Ти си извличащ модул. От разговора по-долу извади САМО трайното.

Върни ЧИСТ JSON (без markdown ограждане), с точно тези ключове:
{
  "decisions":   [{"what": "...", "why": "..."}],
  "preferences": [{"topic": "...", "value": "..."}],
  "threads":     [{"title": "...", "next_step": "..."}]
}

Правила:
- decisions: какво е РЕШЕНО (не какво е обсъдено), с причината.
- preferences: как потребителят иска нещата да се правят — включително корекции,
  които е направил на асистента. Формулирай ги като трайно правило.
- threads: работа, която НЕ е завършена. next_step да е конкретно действие.
- Ако някоя категория е празна — върни празен списък. НЕ измисляй.
- Не включвай еднократни факти, дреболии или неща, верни само за този разговор.
- Пиши на езика, на който говори ПОТРЕБИТЕЛЯТ в разговора (обикновено български).
  Този текст се показва на него в следващата сесия, не на друг модел.
"""


def auto_capture(messages: list[dict], max_chars: int = 6000) -> dict:
    """Извлича трайното от един разговор и го записва — БЕЗ да разчита моделът
    сам да е викал REMEMBER/TASK_ADD по време на чата.

    Защо: тестове на живо показаха, че моделите в безплатната ротация често
    НЕ посягат към memory tool-овете, дори с изрична инструкция — тръгват да
    решават задачата и забравят да запишат. Инструкция не е механизъм. Тук
    записът се случва независимо от поведението на модела по време на чата.

    Никога не хвърля — провалено извличане не бива да чупи изхода от сесия.
    Връща какво е записано (за показване/логване)."""
    written = {"decisions": 0, "preferences": 0, "threads": 0}
    convo = [m for m in messages if m.get("role") in ("user", "assistant") and m.get("content")]
    if len(convo) < 2:
        return written

    text = "\n".join(f"{m['role']}: {str(m['content'])[:1200]}" for m in convo)[-max_chars:]

    # Показваме какво ВЕЧЕ е записано, за да преизползва съществуващите
    # формулировки вместо да измисля синоними ("стил на комуникация" срещу
    # "обяснения" за същото нещо). Това е дедупликацията, която реално работи —
    # LLM-ът разпознава "това е същото" далеч по-надеждно от embedding праг
    # (виж измерванията в _is_semantic_dup).
    known = ""
    try:
        prefs = list_preferences()
        if prefs:
            known += "\nВече записани предпочитания (ако новото е за същото — ползвай СЪЩАТА тема):\n"
            known += "\n".join(f"  - {k}: {v}" for k, v in prefs.items())
        decs = [d["what"] for d in list_decisions(15)]
        if decs:
            known += "\nВече записани решения (НЕ ги повтаряй):\n" + "\n".join(f"  - {d}" for d in decs)
        opens = [t["title"] for t in list_threads("open", 15)]
        if opens:
            known += "\nВече отворени нишки (НЕ ги дублирай):\n" + "\n".join(f"  - {t}" for t in opens)
    except Exception:
        pass

    try:
        import json as _json

        from genesis_agent.brain import Brain

        reply = Brain(min_size_b=32).complete([
            {"role": "system", "content": _CAPTURE_PROMPT + known},
            {"role": "user", "content": text},
        ])
        raw = (reply.raw_text or "").strip()
        if raw.startswith("```"):  # моделите често ограждат JSON-а въпреки инструкцията
            raw = raw.split("```")[1] if "```" in raw[3:] else raw.strip("`")
            raw = raw.removeprefix("json").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end == -1:
            return written
        data = _json.loads(raw[start:end + 1])
    except Exception:
        return written

    try:
        for d in (data.get("decisions") or [])[:10]:
            if isinstance(d, dict) and d.get("what"):
                add_decision(d["what"], d.get("why", ""))
                written["decisions"] += 1
        for p in (data.get("preferences") or [])[:10]:
            if isinstance(p, dict) and p.get("topic") and p.get("value"):
                set_preference(p["topic"], p["value"])
                written["preferences"] += 1
        for t in (data.get("threads") or [])[:10]:
            if isinstance(t, dict) and t.get("title"):
                add_thread(t["title"], t.get("next_step", ""))
                written["threads"] += 1
    except Exception:
        pass
    return written


STALE_DAYS = 10


def _days_since(iso: str) -> int:
    try:
        then = datetime.fromisoformat(iso)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - then).days)
    except Exception:
        return 0


def stale_threads(days: int = STALE_DAYS) -> list[dict]:
    """Отворени нишки, непипани от `days` дни. Хигиена: `auto_capture` създава
    нишки автоматично на всяка компресия и на изход, а 24/7 цикълът пише в тях —
    но НИЩО не ги затваря само. Без периодично разчистване брифингът за няколко
    седмици се превръща в стена от изоставени задачи, т.е. точно "остарял списък,
    по-лош от никакъв". Затова остарелите се показват отделно, с покана да се
    затворят — а не се трият автоматично (може да са важни, просто спрели)."""
    return [t for t in list_threads("open", 100) if _days_since(t["updated_at"]) >= days]


def close_thread(thread_id: Any, drop: bool = False) -> str:
    """Затваря нишка като готова, или я изхвърля (drop=True → трие я напълно).
    Триенето е за нишки, които auto_capture е създал грешно — да не пълнят
    'готови' с неща, които никога не са били реална работа."""
    try:
        tid = int(str(thread_id).strip().lstrip("#"))
    except (ValueError, TypeError):
        return f"[{'DROP' if drop else 'DONE'}] Невалиден id: {thread_id!r}."
    if not drop:
        return update_thread(tid, status="done")
    with _conn() as c:
        cur = c.execute("DELETE FROM threads WHERE id = ?;", (tid,))
        if cur.rowcount == 0:
            return f"[DROP] Няма нишка #{tid}."
    return f"[DROP] ✓ Нишка #{tid} изтрита."


def stats() -> dict:
    with _conn() as c:
        return {
            "open": c.execute("SELECT COUNT(*) FROM threads WHERE status='open';").fetchone()[0],
            "blocked": c.execute("SELECT COUNT(*) FROM threads WHERE status='blocked';").fetchone()[0],
            "done": c.execute("SELECT COUNT(*) FROM threads WHERE status='done';").fetchone()[0],
            "decisions": c.execute("SELECT COUNT(*) FROM decisions;").fetchone()[0],
            "preferences": c.execute("SELECT COUNT(*) FROM preferences;").fetchone()[0],
        }


if __name__ == "__main__":
    print("Статистика:", stats())
    print()
    b = briefing()
    print(b if b else "(празно — още нищо не е записано)")
