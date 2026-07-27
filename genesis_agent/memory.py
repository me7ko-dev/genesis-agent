#!/usr/bin/env python3
"""
genesis_agent/memory.py — Persistent Cross-Session Memory за Genesis Agent.

Cross-session memory interface, свързан директно с episodic_memory.py.

Поддържа:
  - memory_store(key, value)     → запазва произволен ключ/стойност
  - memory_recall(key)           → извлича по ключ
  - memory_search(query)         → семантично търсене в епизодите
  - memory_context(n)            → последните N епизода като контекст за LLM

Съхранение:
  - Прости key/value → SQLite таблица "persistent_memory"
  - Епизоди          → episodic_memory.py (episodes.db)

Употреба:
    from genesis_agent.memory import memory_store, memory_recall, memory_search

    memory_store("user_name", "потребителят")
    memory_store("preferred_language", "bulgarian")

    print(memory_recall("user_name"))   # → "потребителят"

    episodes = memory_search("python retry")
    for ep in episodes:
        print(ep['goal'], ep['similarity'])
"""

from __future__ import annotations

import sqlite3
import json
import datetime
import logging
from pathlib import Path
from typing import Any, Optional, List, Dict

from genesis_agent.config import PROJECT_ROOT
import genesis_agent.episodic_memory as episodic

log = logging.getLogger("genesis.memory")

# ─── SQLite Setup (Key/Value store) ──────────────────────────────────────────

DB_PATH = PROJECT_ROOT / "genesis_agent" / "persistent_memory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_store (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    type  TEXT NOT NULL DEFAULT 'str',
    updated_at TEXT NOT NULL
);
"""


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(_SCHEMA)
    return conn


# Инициализираме DB веднага при import
with _get_conn() as _c:
    pass


# ─── Key/Value операции ───────────────────────────────────────────────────────

def memory_store(key: str, value: Any) -> None:
    """
    Запазва стойност в persistent memory.

    Поддържа: str, int, float, bool, list, dict (всичко JSON-сериализируемо).
    """
    type_name = type(value).__name__
    serialized = json.dumps(value, ensure_ascii=False)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO kv_store (key, value, type, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                                           type=excluded.type,
                                           updated_at=excluded.updated_at;
            """,
            (key, serialized, type_name, now),
        )
    log.debug(f"[memory] Запазено: {key} = {str(value)[:60]}")


def memory_recall(key: str, default: Any = None) -> Any:
    """
    Извлича стойност по ключ. Връща default ако ключът не съществува.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM kv_store WHERE key = ?;", (key,)
        ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return row[0]


def memory_delete(key: str) -> bool:
    """Изтрива ключ от persistent memory. Връща True ако е намерен."""
    with _get_conn() as conn:
        cursor = conn.execute("DELETE FROM kv_store WHERE key = ?;", (key,))
        return cursor.rowcount > 0


def memory_list_keys(prefix: str = "") -> List[str]:
    """Връща всички ключове (с опционален prefix филтър)."""
    with _get_conn() as conn:
        if prefix:
            rows = conn.execute(
                "SELECT key FROM kv_store WHERE key LIKE ? ORDER BY key;",
                (f"{prefix}%",)
            ).fetchall()
        else:
            rows = conn.execute("SELECT key FROM kv_store ORDER BY key;").fetchall()
    return [r[0] for r in rows]


def memory_dump() -> Dict[str, Any]:
    """Връща всички key/value двойки като речник."""
    with _get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM kv_store ORDER BY key;").fetchall()
    result = {}
    for key, val_str in rows:
        try:
            result[key] = json.loads(val_str)
        except Exception:
            result[key] = val_str
    return result


# ─── Episodic Memory интеграция ───────────────────────────────────────────────

def memory_record_episode(
    goal: str,
    outcome: str,
    skill_path: str = "unknown",
    lessons: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
) -> None:
    """Записва епизод в episodic_memory (wrapper за удобство)."""
    episodic.record_episode(
        goal=goal,
        outcome=outcome,
        skill_path=skill_path,
        lessons_learned=lessons,
        tags=tags,
    )
    log.debug(f"[memory] Епизод записан: {goal[:60]}")


def memory_search(query: str, top_k: int = 5) -> List[Dict]:
    """
    Семантично търсене в episodic memory.

    Returns:
        list[dict] с ключове: goal, outcome, timestamp, similarity, lessons_learned
    """
    try:
        return episodic.search_episodes(query, top_k=top_k)
    except Exception as e:
        log.warning(f"[memory] Търсенето неуспешно (scikit-learn наличен ли е?): {e}")
        # Fallback: прост текстов search
        episodes = episodic._fetch_all_episodes()
        query_lower = query.lower()
        matches = [
            ep for ep in episodes
            if query_lower in ep.get("goal", "").lower()
            or query_lower in ep.get("outcome", "").lower()
        ]
        return matches[:top_k]


def memory_context(n: int = 10) -> str:
    """
    Генерира текстов контекст от последните N епизода.
    Предназначен за включване в LLM промпт.

    Returns:
        Форматиран текст готов за вграждане в системен промпт.
    """
    summary = episodic.summarize_sessions(last_n=n)
    keys = memory_list_keys()
    kv_info = ""
    if keys:
        kv_pairs = memory_dump()
        # Показваме само важни ключове (без дълги стойности)
        filtered = {
            k: v for k, v in kv_pairs.items()
            if isinstance(v, (str, int, float, bool)) and len(str(v)) < 200
        }
        if filtered:
            kv_info = "\n\nПерсистентна памет (user preferences):\n"
            kv_info += "\n".join(f"  {k}: {v}" for k, v in filtered.items())

    return f"{summary}{kv_info}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("🧠 Тест: Persistent Key/Value Memory")
    memory_store("user_name", "потребителят")
    memory_store("preferred_language", "bulgarian")
    memory_store("coding_style", {"indentation": 4, "quotes": "double"})
    memory_store("last_project", "genesis-0")

    print(f"  user_name: {memory_recall('user_name')}")
    print(f"  preferred_language: {memory_recall('preferred_language')}")
    print(f"  coding_style: {memory_recall('coding_style')}")
    print(f"  Всички ключове: {memory_list_keys()}")

    print("\n📝 Тест: Записване на епизод")
    memory_record_episode(
        goal="Тест на memory модула",
        outcome="Успешно",
        lessons=["memory_store() работи с всякакви типове"],
        tags=["тест", "памет", "genesis"],
    )

    print("\n🔍 Тест: Семантично търсене")
    results = memory_search("тест памет")
    print(f"  Намерени {len(results)} епизода:")
    for ep in results[:3]:
        sim = ep.get('similarity', 0)
        print(f"  - {ep['goal']} (сходство: {sim:.2f})")

    print("\n📋 Контекст за LLM:")
    print(memory_context(n=5))
