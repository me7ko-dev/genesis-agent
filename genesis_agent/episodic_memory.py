"""
genesis_agent/episodic_memory.py
---------------------------

Persistently stores “episodes” – значими събития, които Genesis Agent преживява.
Всеки епизод съдържа:

    * timestamp      – време на събитието (ISO‑8601)
    * goal           – каква беше целта
    * outcome        – какъв беше резултатът
    * skill_path     – път до използваните умения / модули
    * lessons_learned– списък от научени уроци (текст)
    * tags           – произволни маркери, разделени със запетая

Основните функции:

    * record_episode(goal, outcome, skill_path,
                     lessons_learned=None, tags=None) → None
    * search_episodes(query, top_k=5) → list[dict]
    * get_lessons(topic) → list[str]
    * summarize_sessions(last_n=10) → str

Епизодите се съхраняват в SQLite база данни „episodes.db”.
Търсенето се базира на TF‑IDF векторизация (scikit‑learn) върху
комбинацията от полетата *goal*, *outcome*, *lessons_learned* и *tags*.
"""

import datetime
import json
import sqlite3

# ---------- SQLite setup ----------
from genesis_agent.config import DATA_DIR

DB_PATH = DATA_DIR / "episodes.db"
SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    goal TEXT NOT NULL,
    outcome TEXT NOT NULL,
    skill_path TEXT NOT NULL,
    lessons_learned TEXT,   -- JSON‑encoded list
    tags TEXT               -- comma‑separated string
);
"""

def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def _init_db() -> None:
    with _get_connection() as conn:
        conn.executescript(SCHEMA)

_init_db()   # Ensure the DB exists on import

# ---------- Helper utilities ----------
def _now_iso() -> str:
    """Текущото време във формат ISO‑8601 (UTC)."""
    # timezone‑aware, за да избегнем DeprecationWarning.
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

def _json_encode(lst: list[str] | None) -> str | None:
    return json.dumps(lst, ensure_ascii=False) if lst else None

def _json_decode(s: str | None) -> list[str]:
    return json.loads(s) if s else []

# ---------- Core API ----------
def record_episode(
    goal: str,
    outcome: str,
    skill_path: str,
    lessons_learned: list[str] | None = None,
    tags: list[str] | None = None,
) -> None:
    """Записва нов епизод в базата."""
    timestamp = _now_iso()
    lessons_json = _json_encode(lessons_learned)
    tags_str = ",".join(tags) if tags else None

    with _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO episodes
            (timestamp, goal, outcome, skill_path, lessons_learned, tags)
            VALUES (?, ?, ?, ?, ?, ?);
            """,
            (timestamp, goal, outcome, skill_path, lessons_json, tags_str),
        )
        conn.commit()


def _fetch_all_episodes() -> list[dict]:
    """Връща всички епизоди като списък от речници (за TF‑IDF)."""
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, timestamp, goal, outcome, skill_path,
                   lessons_learned, tags
            FROM episodes
            ORDER BY id;
            """
        ).fetchall()

    episodes = []
    for row in rows:
        ep = {
            "id": row[0],
            "timestamp": row[1],
            "goal": row[2],
            "outcome": row[3],
            "skill_path": row[4],
            "lessons_learned": _json_decode(row[5]),
            "tags": row[6].split(",") if row[6] else [],
        }
        episodes.append(ep)
    return episodes


def search_episodes(query: str, top_k: int = 5) -> list[dict]:
    """
    Търси епизоди, подобни на *query* чрез косинусова сходство на TF‑IDF.
    Връща най‑по‑подобните ``top_k`` епизода.
    """
    # Local import – scikit‑learn е опционална зависимост.
    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    episodes = _fetch_all_episodes()
    if not episodes:
        return []

    corpus = [
        " ".join(
            [
                ep["goal"],
                ep["outcome"],
                " ".join(ep["lessons_learned"]),
                " ".join(ep["tags"]),
            ]
        ).lower()
        for ep in episodes
    ]

    # За български текст не използваме вградените stop‑words.
    vectorizer = TfidfVectorizer(stop_words=None)
    tfidf_matrix = vectorizer.fit_transform(corpus)

    query_vec = vectorizer.transform([query.lower()])
    sims = cosine_similarity(query_vec, tfidf_matrix).flatten()
    top_indices = np.argsort(sims)[::-1][:top_k]

    results = []
    for idx in top_indices:
        ep = episodes[int(idx)].copy()
        ep["similarity"] = float(sims[idx])
        results.append(ep)

    return results


def get_lessons(topic: str) -> list[str]:
    """Връща уникални уроци, съдържащи *topic* в текста."""
    episodes = _fetch_all_episodes()
    matched = set()
    low = topic.lower()
    for ep in episodes:
        for lesson in ep["lessons_learned"]:
            if low in lesson.lower():
                matched.add(lesson.strip())
    return list(matched)


def summarize_sessions(last_n: int = 10) -> str:
    """
    Генерира прост текстов преглед на последните ``last_n`` епизода.
    Използва ASCII стрелка „->“, за да не предизвика UnicodeEncodeError.
    """
    with _get_connection() as conn:
        rows = conn.execute(
            """
            SELECT timestamp, goal, outcome, lessons_learned
            FROM episodes
            ORDER BY id DESC
            LIMIT ?;
            """,
            (last_n,),
        ).fetchall()

    if not rows:
        return "Няма записани епизоди."

    lines = ["Последни епизоди:"]
    for i, (ts, goal, outcome, lessons_json) in enumerate(reversed(rows), 1):
        lessons = _json_decode(lessons_json)
        lesson_part = f" | Урок: {lessons[0]}" if lessons else ""
        lines.append(f"{i}. [{ts}] Цел: {goal} -> Резултат: {outcome}{lesson_part}")

    return "\n".join(lines)


# ---------- Примерно ползване ----------
if __name__ == "__main__":
    # Примерен запис
    record_episode(
        goal="Разработване на нов модул за обработка на текст",
        outcome="Успешно създаден прототип",
        skill_path="genesis_agent.modules.text_processing",
        lessons_learned=["Трябва да се внимава с кодиране на Unicode"],
        tags=["разработка", "модул", "текст"],
    )

    # Търсене
    print("\nТърсене по ключова дума 'unicode':")
    for ep in search_episodes("unicode"):
        print(f"- [{ep['timestamp']}] {ep['goal']} (сходство: {ep['similarity']:.2f})")

    # Уроци по тема
    print("\nУроци за 'unicode':", get_lessons("unicode"))

    # Обобщение
    print("\nОбобщение на последните 5 сесии:")
    print(summarize_sessions(5))
