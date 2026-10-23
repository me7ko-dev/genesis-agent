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

Нишките и решенията са на ПАПКАТА, в която се работи (колона `workspace`);
предпочитанията са общи. До 2026-09-26 всичко беше общо, а инсталираният
Genesis работи там, където е пуснат — нова празна папка получаваше „Взети
решения: създаден money.py …“ от друг проект, моделът търсеше несъществуващия
файл и спираше на пазача за повторения, без да напише ред (bench_projects,
euro-convert). Старите записи без папка не се показват никъде — не се знае
на кой проект са.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from genesis_agent import sqlite_store
from genesis_agent.config import MEMORY_DIR
from genesis_agent.notifier import API_TOKEN_RE

log = logging.getLogger("genesis.workspace_memory")

DB_PATH = MEMORY_DIR / "workspace_memory.db"

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


_workspace: str | None = None


def _norm(path: Any) -> str:
    """Един и същ низ за една и съща папка: абсолютен път, без разлика в
    регистъра на Windows (`C:\\Users` и `c:\\users` е едно място)."""
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    return os.path.normcase(str(p))


def set_workspace(path: Any) -> None:
    """Папката, чиито нишки и решения се четат и пишат. Вика се от
    genesis_skills.set_workspace — там, където инструментите получават своята."""
    global _workspace
    _workspace = _norm(path) if path else None


def current_workspace() -> str:
    if _workspace is None:
        from genesis_agent.paths import workspace_dir
        return _norm(workspace_dir())
    return _workspace


def _conn() -> sqlite3.Connection:
    # Повреден файл отива настрани като .corrupt вместо всеки REMEMBER/TASK_ADD
    # завинаги да връща „file is not a database“ (одит 2026-10-22).
    conn = sqlite_store.connect(DB_PATH, _SCHEMA, timeout=10, wal=True)
    for table in ("threads", "decisions"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table});")}
        if "workspace" not in cols:
            # Два процеса върху нова/стара база виждат колоната липсваща и
            # двата; вторият ALTER гърмеше и неговият REMEMBER се губеше
            # (одит 2026-10-22: 8 процеса върху нова база → 2 грешки).
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN workspace TEXT NOT NULL DEFAULT '';")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e):
                    conn.close()
                    raise
    return conn


@contextmanager
def _write() -> Iterator[sqlite3.Connection]:
    """Транзакция, която заключва за запис ПРЕДИ проверката за дубликат.

    sqlite3 отваря транзакцията чак при INSERT, тоест SELECT-ът за дубликат
    и записът бяха разделени: два процеса (два терминала, терминал + телефон,
    auto_capture на изход и в двата) пропускаха реда на другия и записваха и
    двата. Одит 2026-10-22: 8 процеса × 5 заглавия → 14 нишки и 11 решения
    вместо 5 и 5."""
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE;")
        with conn:
            yield conn
    finally:
        conn.close()


def _clean(text: Any, limit: int = _MAX_TEXT) -> str:
    return str(text or "").strip()[:limit]


# ─── Тайни ───────────────────────────────────────────────────────────────────
# Одит 2026-10-22, възпроизведено: REMEMBER „stripe: ключът е sk_live_…“ влизаше
# дословно в брифинга — в системния промпт на ВСЕКИ проект (предпочитанията са
# общи), пратен до доставчика, който обслужва хода, — а решение „деплой с
# GITHUB_TOKEN=ghp_…“ — в GENESIS_REPORT.md на архива от `genesis export`.
# notifier.redact е за известия, където да хване повече не вреди; тук
# „path: src/app.py“ и „basic auth за админа“ са истински решения, затова се
# маскират само формите, които не са текст: познатите ключове, Stripe, парола
# в адрес, Bearer и ИМЕ_С_KEY/TOKEN/SECRET/PASSWORD=стойност — стойност с цифра
# или от 20+ знака (тайната е случаен низ; `sort_key=created_at` е решение,
# `API_KEY=$API_KEY` — препратка).
_SECRET_RES = (
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 ***"),
    (re.compile(r"(?i)\b((?:[a-z0-9]+_)*(?:api_?key|key|token|secret|password|passwd)(?:_[a-z0-9]+)*)"
                r"\s*=\s*(?![$<{%*])(?=\S*\d|\S{20})\S{6,}"), r"\1=***"),
    (re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{10,}"), "***"),
    (API_TOKEN_RE, "***"),
)
_SECRET_NOTE = (" ⚠️ Ключът/паролата е заменен(а) с *** — не пазя тайни в паметта (тя влиза в "
                "промпта на всеки проект и в `genesis export`); сложи го в .env.")


def mask_secrets(text: Any) -> str:
    """Текстът с *** на мястото на ключове, токени и пароли в адреси."""
    out = str(text or "")
    for rx, repl in _SECRET_RES:
        out = rx.sub(repl, out)
    return out


def _masked(*texts: str) -> tuple[list[str], bool]:
    out = [mask_secrets(t) for t in texts]
    return out, out != list(texts)


# ─── Нишки работа ────────────────────────────────────────────────────────────

def add_thread(title: str, next_step: str = "", notes: str = "") -> str:
    """Отваря нова нишка работа. Дедуплицира по заглавие (case-insensitive) —
    LLM-ът лесно би добавил същата задача два пъти в различни сесии."""
    title = _clean(title, 300)
    if not title:
        return "[TASK_ADD] Празно заглавие — нищо не е добавено."
    (title, next_step, notes), masked = _masked(title, _clean(next_step), _clean(notes))
    note = _SECRET_NOTE if masked else ""
    ws = current_workspace()
    with _write() as c:
        # Сравнение в Python, не със SQL lower(): той сгъва само ASCII и
        # „Миграция“/„миграция“ ставаха две нишки. Затворена нишка със същото
        # заглавие е стара работа — новата задача е отворена, не „вече съществува“.
        key = title.casefold()
        row = next((r for r in c.execute(
            "SELECT id, status, title FROM threads WHERE workspace = ? AND status != 'done';",
            (ws,)) if r[2].casefold() == key), None)
        if row:
            # Съществува — обновяваме следващата стъпка вместо да дублираме.
            # Празна нова стъпка не трие записаната (auto_capture подава "").
            if _clean(next_step):
                c.execute("UPDATE threads SET next_step = ?, updated_at = ? WHERE id = ?;",
                          (_clean(next_step), _now(), row[0]))
            return f"[TASK_ADD] Нишка #{row[0]} вече съществува ({row[1]}) — обнових следващата стъпка.{note}"
        cur = c.execute(
            "INSERT INTO threads (title, status, next_step, notes, created_at, updated_at, workspace)"
            " VALUES (?, 'open', ?, ?, ?, ?, ?);",
            (title, _clean(next_step), _clean(notes), _now(), _now(), ws),
        )
        return f"[TASK_ADD] ✓ Нишка #{cur.lastrowid}: {title}{note}"


def update_thread(thread_id: Any, status: str = "", next_step: str = "", notes: str = "") -> str:
    """Обновява статус/следваща стъпка. Празен аргумент = не пипай това поле."""
    try:
        tid = int(str(thread_id).strip().lstrip("#"))
    except (ValueError, TypeError):
        return f"[TASK_UPDATE] Невалиден id: {thread_id!r} (очаквам число, виж TASK_LIST)."
    status = _clean(status, 20).lower().replace("-", "_").replace(" ", "_")
    # Думите на TODO_WRITE и близките им: проба 2026-10-12 — gpt-oss-120b
    # пишеше "completed" и губеше извикване за отказа.
    status = {"completed": "done", "complete": "done", "finished": "done", "closed": "done",
              "in_progress": "open", "pending": "open", "todo": "open", "active": "open",
              "stuck": "blocked", "waiting": "blocked"}.get(status, status)
    if status and status not in STATUSES:
        return f"[TASK_UPDATE] Невалиден статус {status!r}. Позволени: {', '.join(STATUSES)}."
    (next_step, notes), masked = _masked(str(next_step or ""), str(notes or ""))
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
        params.append(current_workspace())
        cur = c.execute(f"UPDATE threads SET {', '.join(sets)} WHERE id = ? AND workspace = ?;", params)
        if cur.rowcount == 0:
            return f"[TASK_UPDATE] Няма нишка #{tid}."
    return f"[TASK_UPDATE] ✓ Нишка #{tid} обновена." + (_SECRET_NOTE if masked else "")


def list_threads(status: str = "open", limit: int = 20) -> list[dict]:
    q = "SELECT id, title, status, next_step, notes, updated_at FROM threads WHERE workspace = ?"
    params: list = [current_workspace()]
    if status and status != "all":
        q += " AND status = ?"; params.append(status)
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
    n_new = _same_text(text)
    for e in existing:
        if _same_text(e) == n_new:
            return e
    return None


def _same_text(s: str) -> str:
    # Регистър, интервали и точката накрая — нищо повече. Махането на всички
    # знаци сливаше „C#“ с „C++“, „1.5 секунди“ с „15 секунди“, „<= 4“ с „>= 4“.
    return " ".join(s.casefold().split()).rstrip(".!;, ")


def add_decision(what: str, why: str = "") -> str:
    what = _clean(what, 500)
    if not what:
        return "[REMEMBER] Празно решение — нищо не е записано."
    (what, why), masked = _masked(what, _clean(why, 500))
    note = _SECRET_NOTE if masked else ""
    ws = current_workspace()
    # Дедупликация: auto_capture върви и при компресия, и на изход, често върху
    # застъпващо се съдържание — без това едно решение влиза 2-3 пъти с леко
    # различна формулировка и брифингът бързо става шум.
    with _write() as c:
        existing = [r[0] for r in c.execute(
            "SELECT what FROM decisions WHERE workspace = ? ORDER BY id DESC LIMIT 50;", (ws,))]
        dup = _is_semantic_dup(what, existing)
        if dup:
            return f"[REMEMBER] Вече е записано (като «{dup[:60]}») — пропуснато.{note}"
        c.execute("INSERT INTO decisions (what, why, created_at, workspace) VALUES (?, ?, ?, ?);",
                  (what, why, _now(), ws))
    return f"[REMEMBER] ✓ Записано решение: {what[:80]}{note}"


def set_preference(topic: str, value: str) -> str:
    topic, value = _clean(topic, 100), _clean(value, 500)
    if not topic or not value:
        return "[REMEMBER] Нужни са и тема, и стойност."
    (topic, value), masked = _masked(topic, value)
    note = _SECRET_NOTE if masked else ""
    with _write() as c:
        existing = dict(c.execute("SELECT topic, value FROM preferences;").fetchall())
        # Същата тема в друг регистър е СЪЩАТА тема. PRIMARY KEY-ят различава
        # „език на комитите“ от „Език на комитите“ и брифингът показваше и
        # старото „английски“, и новото „само български“ (одит 2026-10-22).
        twins = [t for t in existing if _same_text(t) == _same_text(topic)]
        same = twins[0] if twins else None
        # Двойниците, записани преди поправката, остават в промпта с
        # противоречащите си стари стойности — новата стойност е и за тях
        # (ревю 2026-10-22).
        c.executemany("DELETE FROM preferences WHERE topic = ?;", [(t,) for t in twins[1:]])
        if same is None:
            # Същото предпочитание често идва под РАЗЛИЧНА тема ("стил на
            # комуникация" vs "обяснения") — затова сравняваме и по стойност.
            dup_val = _is_semantic_dup(value, list(existing.values()))
            old_topic = next((t for t, v in existing.items() if v == dup_val), None) if dup_val else None
            if old_topic:
                # Обновяваме съществуващия запис вместо да добавяме синоним.
                c.execute("UPDATE preferences SET value = ?, updated_at = ? WHERE topic = ?;",
                          (value, _now(), old_topic))
                return f"[REMEMBER] Обединено със съществуващото «{old_topic}».{note}"
            same = topic
        c.execute(
            "INSERT INTO preferences (topic, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(topic) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at;",
            (same, value, _now()),
        )
    return f"[REMEMBER] ✓ Предпочитание «{same}»: {value[:80]}{note}"


def list_decisions(limit: int = 10) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT what, why, created_at FROM decisions WHERE workspace = ?"
            " ORDER BY id DESC LIMIT ?;",
            (current_workspace(), int(limit)),
        ).fetchall()
    return [{"what": r[0], "why": r[1], "created_at": r[2]} for r in rows]


def list_preferences() -> dict[str, str]:
    with _conn() as c:
        rows = c.execute("SELECT topic, value FROM preferences ORDER BY topic;").fetchall()
    return {r[0]: r[1] for r in rows}


# ─── Брифинг (това се инжектира в промпта) ───────────────────────────────────

# Предпочитанията са общи и влизат в промпта на всяка заявка във всеки проект,
# а fit_system_prompt() минава преди брифингът да се залепи — нищо не ги реже
# по-късно. Одит 2026-10-22: 300 записа → 100 128 знака брифинг. Най-новите
# горе; таванът е и по брой, и по знаци (една стойност е до 500).
_MAX_PREFS = 20
_PREFS_CHARS = 3000


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
        # mask_secrets и при четене: записите отпреди 2026-10-22 са в базата
        # такива, каквито са дошли.
        parts.append("Отворена работа:\n" + mask_secrets("\n".join(lines)))

    if stale:
        lines = [f"  #{t['id']} {t['title'][:70]} ({_days_since(t['updated_at'])}д)"
                 for t in stale[:8]]
        extra = f"\n  … и още {len(stale) - 8}" if len(stale) > 8 else ""
        parts.append(f"Заспали нишки (>{STALE_DAYS} дни без движение — питай потребителят дали "
                     f"да се затворят, не ги подхващай сам):\n" + mask_secrets("\n".join(lines)) + extra)

    decisions = list_decisions(max_decisions)
    if decisions:
        lines = [f"  • {d['what']}" + (f" (защото: {d['why']})" if d["why"] else "")
                 for d in decisions]
        parts.append("Взети решения (не ги предоговаряй без причина):\n" + mask_secrets("\n".join(lines)))

    with _conn() as c:
        prefs = c.execute("SELECT topic, value FROM preferences"
                          " ORDER BY updated_at DESC, rowid DESC;").fetchall()
    if prefs:
        lines, used = [], 0
        for k, v in prefs:
            line = mask_secrets(f"  • {k}: {v}")
            if lines and (len(lines) >= _MAX_PREFS or used + len(line) > _PREFS_CHARS):
                break
            lines.append(line)
            used += len(line)
        if len(prefs) > len(lines):
            lines.append(f"  … и още {len(prefs) - len(lines)} по-стари (не са в промпта)")
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
- preferences: как потребителят иска нещата да се правят ВЪВ ВСЕКИ проект (език,
  стил, начин на работа) — включително корекции, които е направил на асистента.
  Формулирай ги като трайно правило. Имена на файлове, функции, модули и
  изисквания на ТОЗИ проект не са предпочитания — те са decisions.
- threads: работа, която НЕ е завършена. next_step да е конкретно действие.
- Ако някоя категория е празна — върни празен списък. НЕ измисляй.
- Не включвай еднократни факти, дреболии или неща, верни само за този разговор.
- Пиши на езика, на който говори ПОТРЕБИТЕЛЯТ в разговора (обикновено български).
  Този текст се показва на него в следващата сесия, не на друг модел.
"""


# Preferences go into EVERY later session, so the extractor's word is not
# enough: on 2026-09-28 one site task became 11 "preferences" in one pass
# (index.html/styles.css, SVG, SEO, "premium modern"), and a Python task the
# next day opened with them. A preference is kept only when the operator said
# something general, and a pass that yields this many is a project's spec.
_GENERAL_MARKERS = ("винаги", "никога", "всеки път", "от сега", "отсега", "занапред",
                    "предпочитам", "не обичам", "не искам", "always", "never",
                    "from now on", "every time", "i prefer")
_MAX_PREFS_PER_CAPTURE = 2


# Чатът лепи провереното знание от библиотеката под заявката на оператора
# (run_turn → domain_context). То НЕ е казано от оператора: 15K знака уеб
# ръководство съдържат „никога“ и отключваха глобални предпочитания от една
# задача за сайт (одит 2026-10-07 — точно случаят от 2026-09-28).
_INJECTED = re.compile(r"\n\n## Проверено (ръководство|знание) от библиотеката:.*", re.DOTALL)


def operator_text(content: Any) -> str:
    """Само каквото операторът е написал — без приложеното знание. Картинката е
    „[картинка: име]“, не base64 в промпта за паметта (2026-10-15)."""
    from genesis_agent.vision import text_of
    return _INJECTED.sub("", text_of(content))


def _operator_spoke_generally(convo: list[dict]) -> bool:
    said = " ".join(operator_text(m["content"]).lower() for m in convo if m.get("role") == "user")
    return any(marker in said for marker in _GENERAL_MARKERS)


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

    text = "\n".join(f"{m['role']}: {(operator_text(m['content']) if m['role'] == 'user' else str(m['content']))[:1200]}"
                     for m in convo)[-max_chars:]

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
        log.debug("известните решения/нишки за промпта не се прочетоха", exc_info=True)

    try:
        import json as _json

        from genesis_agent.brain import Brain

        reply = Brain(min_size_b=32, light=True).complete([
            {"role": "system", "content": _CAPTURE_PROMPT + mask_secrets(known)},
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
        # A real failure here (model unreachable, rate-limited, bad JSON)
        # looks identical to "genuinely nothing new to remember" without
        # this — the only user-visible symptom was the absence of a line
        # in the session summary. Log it so the two are distinguishable.
        log.warning("auto_capture: extraction failed, nothing written this round", exc_info=True)
        return written

    try:
        for d in (data.get("decisions") or [])[:10]:
            if isinstance(d, dict) and d.get("what"):
                add_decision(d["what"], d.get("why", ""))
                written["decisions"] += 1
        new_prefs = [p for p in (data.get("preferences") or [])
                     if isinstance(p, dict) and p.get("topic") and p.get("value")]
        if len(new_prefs) > _MAX_PREFS_PER_CAPTURE or not _operator_spoke_generally(convo):
            if new_prefs:
                log.info("auto_capture: %d preference(s) not kept — a task's requirements, "
                         "not something the operator said in general", len(new_prefs))
            new_prefs = []
        for p in new_prefs:
            set_preference(p["topic"], p["value"])
            written["preferences"] += 1
        for t in (data.get("threads") or [])[:10]:
            if isinstance(t, dict) and t.get("title"):
                add_thread(t["title"], t.get("next_step", ""))
                written["threads"] += 1
    except Exception:
        log.warning("auto_capture: writing extracted memory failed partway through "
                    "(written so far: %r)", written, exc_info=True)
    return written


STALE_DAYS = 10


def _days_since(iso: str) -> int:
    try:
        then = datetime.fromisoformat(iso)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return max(0, (datetime.now(timezone.utc) - then).days)
    except Exception:
        log.debug("невалидна дата на нишка — 0 дни", exc_info=True)
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
        # Само в ТОЗИ workspace: `/tasks` показва само неговите нишки, а номер
        # от друг проект се триеше тихо (одит 2026-10-07).
        cur = c.execute("DELETE FROM threads WHERE id = ? AND workspace = ?;",
                        (tid, current_workspace()))
        if cur.rowcount == 0:
            return f"[DROP] Няма нишка #{tid}."
    return f"[DROP] ✓ Нишка #{tid} изтрита."


def stats() -> dict:
    ws = (current_workspace(),)
    count = "SELECT COUNT(*) FROM threads WHERE workspace = ? AND status = '{}';"
    with _conn() as c:
        return {
            "open": c.execute(count.format("open"), ws).fetchone()[0],
            "blocked": c.execute(count.format("blocked"), ws).fetchone()[0],
            "done": c.execute(count.format("done"), ws).fetchone()[0],
            "decisions": c.execute("SELECT COUNT(*) FROM decisions WHERE workspace = ?;", ws).fetchone()[0],
            "preferences": c.execute("SELECT COUNT(*) FROM preferences;").fetchone()[0],
        }


if __name__ == "__main__":
    print("Статистика:", stats())
    print()
    b = briefing()
    print(b if b else "(празно — още нищо не е записано)")
