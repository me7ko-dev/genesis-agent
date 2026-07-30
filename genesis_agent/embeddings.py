#!/usr/bin/env python3
"""
genesis_agent.embeddings — семантична памет: локални embeddings вместо keyword search.

Проблемът, който решава: skill_loader.search_skills и goal_engine.next_goals
работят по КЛЮЧОВИ ДУМИ — "reverse a string" и "invert character order in text"
изглеждат различни, макар да значат едно и също → дублиращи умения, пропуснато
преизползване. Тук вместо това всяко умение/цел се представя като вектор
(embedding), и близостта се мери по смисъл (cosine similarity), не по думи.

Модел: Ollama `nomic-embed-text` (~274MB, локален, безплатен, бърз). Индексът е
обикновен numpy масив в SQLite blob — 2148 умения е малко, brute-force cosine
е <10ms, не трябват FAISS/vector DB (излишна сложност за този мащаб).

Публичен интерфейс:
    embed(text) -> list[float]                         # един вектор
    index_skill(name, text)                             # добавя/обновява в индекса
    semantic_search(query, top_k=5) -> list[(name, score)]
    semantic_duplicate(text, threshold=0.92) -> str|None  # намира близък дубликат
    reindex_all()                                        # пълно преиндексиране
"""
from __future__ import annotations

import json
import sqlite3
import struct

import requests

from genesis_agent.config import DATA_DIR

DB_PATH = DATA_DIR / "embeddings.db"
MODEL = "nomic-embed-text"
_OLLAMA_URL = "http://localhost:11434/api/embeddings"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    name TEXT PRIMARY KEY,
    vector BLOB NOT NULL,
    dim INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL;")
    c.executescript(_SCHEMA)
    return c


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes, dim: int) -> list[float]:
    return list(struct.unpack(f"{dim}f", blob))


def available() -> bool:
    """Проверява дали Ollama и embedding моделът са налични."""
    try:
        r = requests.get("http://localhost:11434/api/tags", timeout=2)
        if r.status_code != 200:
            return False
        names = [m.get("name", "") for m in r.json().get("models", [])]
        return any(MODEL in n for n in names)
    except Exception:
        return False


def embed(text: str, timeout: int = 60) -> list[float] | None:
    """Връща embedding вектор за текста, или None ако моделът не е наличен.

    timeout=60 по подразбиране — студеният старт на модела на този GPU отнема
    ~20-25с; след първата заявка последващите са бързи (<1с).
    """
    try:
        r = requests.post(_OLLAMA_URL, json={"model": MODEL, "prompt": text[:4000]}, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json().get("embedding")
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def index_skill(name: str, text: str) -> bool:
    """Изчислява и записва embedding за умение/текст. True при успех."""
    vec = embed(text)
    if not vec:
        return False
    import datetime
    with _conn() as c:
        c.execute(
            "INSERT INTO embeddings (name, vector, dim, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET vector=excluded.vector, dim=excluded.dim, updated_at=excluded.updated_at",
            (name, _pack(vec), len(vec), datetime.datetime.now(datetime.timezone.utc).isoformat()),
        )
    return True


def _all_vectors() -> list[tuple[str, list[float]]]:
    with _conn() as c:
        rows = c.execute("SELECT name, vector, dim FROM embeddings").fetchall()
    return [(name, _unpack(blob, dim)) for name, blob, dim in rows]


def semantic_search(query: str, top_k: int = 5) -> list[tuple[str, float]]:
    """Топ-k най-близки умения по смисъл. Празно ако embeddings недостъпни."""
    qvec = embed(query)
    if not qvec:
        return []
    scored = [(name, _cosine(qvec, vec)) for name, vec in _all_vectors()]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def semantic_duplicate(text: str, threshold: float = 0.92) -> str | None:
    """
    Връща името на най-близкото умение ако прилича достатъчно (>= threshold) —
    за да не създаваме дублиращо умение с друга формулировка на същата идея.
    """
    hits = semantic_search(text, top_k=1)
    if hits and hits[0][1] >= threshold:
        return hits[0][0]
    return None


def reindex_all(progress_every: int = 100) -> int:
    """Преиндексира всички умения от skills.json. Връща брой индексирани."""
    from genesis_agent.config import SKILLS_DIR
    idx_path = SKILLS_DIR / "skills.json"
    if not idx_path.exists():
        return 0
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    count = 0
    for i, s in enumerate(idx.get("skills", [])):
        text = f"{s['name'].replace('_', ' ')}. {s.get('description', '')}"
        if index_skill(s["name"], text):
            count += 1
        if (i + 1) % progress_every == 0:
            print(f"  ... индексирани {i + 1} ({count} успешни)")
    return count


if __name__ == "__main__":
    print("embeddings достъпни:", available())
    if available():
        ok = index_skill("_selftest_reverse_a_string", "reverse a string, invert character order")
        print("тест индексиране:", ok)
        hits = semantic_search("invert the order of characters in text", top_k=3)
        print("семантично търсене за перифразирана заявка:")
        for name, score in hits:
            print(f"  {score:.3f}  {name}")
        dup = semantic_duplicate("flip a string backwards")
        print("semantic_duplicate('flip a string backwards'):", dup)
