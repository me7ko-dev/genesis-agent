#!/usr/bin/env python3
"""
genesis_agent.embeddings — семантична памет: локални embeddings вместо keyword search.

Проблемът, който решава: skill_loader.search_skills и goal_engine.next_goals
работят по КЛЮЧОВИ ДУМИ — "reverse a string" и "invert character order in text"
изглеждат различни, макар да значат едно и също → дублиращи умения, пропуснато
преизползване. Тук вместо това всяко умение/цел се представя като вектор
(embedding), и близостта се мери по смисъл (cosine similarity), не по думи.

Модел: Ollama `bge-m3` (~1.2GB, локален, безплатен, многоезичен). Индексът е
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
import logging
import sqlite3
import struct

import requests

from genesis_agent.config import DATA_DIR, SKILLS_DIR

log = logging.getLogger("genesis.embeddings")

DB_PATH = DATA_DIR / "embeddings.db"
SKILLS_INDEX = SKILLS_DIR / "skills.json"
# bge-m3 вместо nomic-embed-text (2026-09-24, scripts/bench_embed.py): 21 умения,
# 42 заявки на български и на латиница, 8 без умение. Верното умение първо:
# 26% → 79%, в първите три: 45% → 95%. nomic е предимно английски.
MODEL = "bge-m3"
# Прагът е на модела: bge-m3 дава по-ниски числа. Там най-високото за заявка
# без умение е 0.46, верните са с медиана 0.51. При 0.50 минават 57% от верните,
# фалшиви 0/8. nomic при старите 0.55 пускаше 14%, също с 0 фалшиви.
SEARCH_THRESHOLD = 0.50
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
    # С кой модел и от какъв текст е векторът (одит 2026-10-07): bge-m3 →
    # mxbai-embed-large са 1024 измерения и двата, верното умение падна от
    # 0.94 на 0.01 без предупреждение; сменено описание не се преизчисляваше.
    cols = {row[1] for row in c.execute("PRAGMA table_info(embeddings)")}
    for col in ("model", "text_hash"):
        if col not in cols:
            c.execute(f"ALTER TABLE embeddings ADD COLUMN {col} TEXT")
    return c


def _hash(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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
    """Косинусова близост. 0.0 за вектори, които НЕ са сравними.

    Дължината се проверява, защото `zip` реже до по-късия мълчаливо (bug fix,
    2026-09-20). Таблицата пази `dim` за всеки ред поотделно, тоест вектори от
    различни модели могат да съжителстват — смени се `MODEL`, или Ollama
    издаде `nomic-embed-text` с друга размерност, и заявка от 768 измерения се
    сравняваше с 384-мерен запис по първите 384 числа. Това не е "по-слабо
    съвпадение", а число без смисъл: измерено между два несвързани случайни
    вектора дава 0.50 при праг за семантично попадение 0.55 в
    skill_loader.search_skills — тоест чист шанс решава дали умение ще бъде
    предложено. По-скъпо е в semantic_duplicate (праг 0.92), където фалшиво
    съвпадение спира записването на ново умение с "вече има такова".
    """
    if len(a) != len(b):
        return 0.0
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
            "INSERT INTO embeddings (name, vector, dim, updated_at, model, text_hash) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET vector=excluded.vector, dim=excluded.dim, "
            "updated_at=excluded.updated_at, model=excluded.model, text_hash=excluded.text_hash",
            (name, _pack(vec), len(vec), datetime.datetime.now(datetime.timezone.utc).isoformat(),
             MODEL, _hash(text)),
        )
    return True


def _count_vectors() -> int:
    with _conn() as c:
        return int(c.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0])


def _all_vectors(model: str | None = None) -> list[tuple[str, list[float]]]:
    """Векторите; с `model` — само от него (записите без модел са отпреди
    колоната и не се знае от кой са — те се преизчисляват от _index_missing)."""
    with _conn() as c:
        if model is None:
            rows = c.execute("SELECT name, vector, dim FROM embeddings").fetchall()
        else:
            rows = c.execute("SELECT name, vector, dim FROM embeddings WHERE model = ?",
                             (model,)).fetchall()
    return [(name, _unpack(blob, dim)) for name, blob, dim in rows]


def semantic_search(query: str, top_k: int = 5) -> list[tuple[str, float]]:
    """Топ-k най-близки умения по смисъл. Празно ако embeddings недостъпни."""
    qvec = embed(query)
    if not qvec:
        return []
    # Записи с друга размерност са от друг модел — несравними, не "далечни".
    # Изключват се от класирането, вместо да висят с нула: така броят им е
    # видим и наличието на стар индекс не изглежда като "няма съвпадения".
    _index_missing(len(qvec))
    usable = [(name, vec) for name, vec in _all_vectors(MODEL) if len(vec) == len(qvec)]
    skipped = _count_vectors() - len(usable)
    if skipped > 0:
        log.warning(
            "%d записа в индекса са с друга размерност от текущия модел (%s) и се "
            "пропускат — пусни reindex_all(), за да се преизчислят.", skipped, MODEL)
    scored = [(name, _cosine(qvec, vec)) for name, vec in usable]
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


def _skill_texts() -> dict[str, str]:
    """Име → текстът, по който се индексира умението (от skills.json)."""
    try:
        idx = json.loads(SKILLS_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {s["name"]: f"{s['name'].replace('_', ' ')}. {s.get('description', '')}"
            for s in idx.get("skills", []) if s.get("name")}


def _index_missing(dim: int) -> int:
    """Индексира уменията без верен вектор: липсващ, от друг модел или
    размерност, или от стар текст (сменено описание). Маха редовете на
    изтрити/преименувани умения. Връща броя преизчислени.

    Индексът се пълнеше само при запис на НОВО умение, а `reindex_all()` не се
    вика от никъде. Измерено на лаптопа (2026-09-24): embeddings.db липсваше
    изцяло, тоест семантичното търсене никога не е намирало нищо. Смяна на
    модела оставя същото: старите вектори са с друга размерност. Тук веднъж
    се наваксва: 21 умения × ~0.3 s при bge-m3. После вече няма липсващи.
    """
    texts = _skill_texts()
    _drop_ghosts(texts)
    with _conn() as c:
        have = {name: (model, h, d) for name, model, h, d in
                c.execute("SELECT name, model, text_hash, dim FROM embeddings")}
    count = 0
    for name, text in texts.items():
        if have.get(name) != (MODEL, _hash(text), dim) and index_skill(name, text):
            count += 1
    return count


def _drop_ghosts(texts: dict[str, str]) -> None:
    """Редовете на умения, които вече ги няма в skills.json. Те заемаха
    местата в top_n*3 на search_skills: с 15 призрака търсенето връщаше []
    (одит 2026-10-07). Нечетим/празен индекс → нищо не се трие."""
    if not texts:
        return
    with _conn() as c:
        names = [n for (n,) in c.execute("SELECT name FROM embeddings")]
        gone = [n for n in names if n not in texts]
        c.executemany("DELETE FROM embeddings WHERE name = ?", [(n,) for n in gone])


def reindex_all(progress_every: int = 100) -> int:
    """Преиндексира всички умения от skills.json. Връща брой индексирани."""
    texts = _skill_texts()
    _drop_ghosts(texts)
    count = 0
    for i, (name, text) in enumerate(texts.items()):
        if index_skill(name, text):
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
