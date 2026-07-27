# genesis_agent/conversation_memory.py
"""
Управление на постоянна история на разговорите за Genesis Agent.
Храни съобщенията в SQLite база (таблица: conversations) и автоматично
сумаризира старата част от контекста, за да запази паметта при дълги
сесии.

Функции:
    add_message(role, content)          – запазва ново съобщение.
    get_history(last_n=20)              – връща последните N съобщения.
    summarize_old_context(threshold=50) – ако броят съобщения надхвърля
                                          threshold, заменя старите със
                                          едно резюме.
    clear_session()                     – изчиства цялата история.
"""

import sqlite3
import os
import datetime
from typing import List, Dict

from genesis_agent.config import DATA_DIR

DB_PATH = str(DATA_DIR / "conversation_memory.db")

# --------------------------------------------------------------
# Инициализация на базата
# --------------------------------------------------------------
def _init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            role      TEXT NOT NULL,
            content   TEXT NOT NULL,
            ts        DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()
    return conn

# --------------------------------------------------------------
# Вътрешна помощна функция за сумиране (placeholder)
# --------------------------------------------------------------
def _simple_summarize(messages: List[Dict[str, str]]) -> str:
    """
    Проста (плейхолдър) функция за обобщаване.
    Тя взема списъка от съобщения и създава кратко резюме,
    което може да бъде заменен с истинска AI‑моделна функция.
    """
    # Базово резюме – броят съобщения и уникалните роли.
    roles = {msg["role"] for msg in messages}
    summary = (
        f"[Context summary] {len(messages)} messages from roles: "
        + ", ".join(sorted(roles))
    )
    # Добавяме част от съдържанието, за да е по‑информативно.
    # Ограничаваме до максимум 300 знака.
    combined = " ".join(msg["content"] for msg in messages)
    summary += " | " + combined[:300].replace("\n", " ").strip()
    return summary

# --------------------------------------------------------------
# Основни публични функции
# --------------------------------------------------------------
def add_message(role: str, content: str) -> None:
    """
    Добавя ново съобщение в базата и проверява дали е необходимо
    автоматично обобщаване.
    """
    conn = _init_db()
    conn.execute(
        "INSERT INTO conversations (role, content) VALUES (?, ?);",
        (role, content),
    )
    conn.commit()
    conn.close()

    # След добавяне проверяваме дали е необходимо обобщение.
    summarize_old_context()


def get_history(last_n: int = 20) -> List[Dict[str, str]]:
    """
    Връща последните `last_n` съобщения в хронологичен ред
    (от най-старото към най-новото).
    """
    conn = _init_db()
    cur = conn.execute(
        """
        SELECT role, content FROM conversations
        ORDER BY id DESC
        LIMIT ?;
        """,
        (last_n,),
    )
    rows = cur.fetchall()
    conn.close()

    # Връщаме в обратен ред, за да е от най-старото към най‑новото.
    history = [{"role": r[0], "content": r[1]} for r in reversed(rows)]
    return history


def summarize_old_context(threshold: int = 50, keep: int | None = None) -> None:
    """
    Ако броят на съобщенията надвише `threshold`, обобщава най-старите НАВЕДНЪЖ
    до `keep` (по подразбиране threshold-20 буфер), вместо до самия threshold.

    Открито 2026-07-25 (scripts/e2e_integration_test.py тест 5, "pre-existing
    unrelated failure" в няколко commit съобщения): при компресия ДО threshold
    точно, веднъж базата стигне >threshold, ВСЯКО следващо add_message() веднага
    пак пресича прага с точно 1 съобщение → summarize изтрива 1-2 стари + добавя
    1 резюме → нетната бройка не мърда напред. get_history() изглеждаше "замръзнала"
    (51→51), макар съобщенията реално да се пишеха и четяха коректно — компресията
    просто ядеше точно толкова, колкото добавянето растеше, на всяко съобщение.
    С буфер (compact до threshold-20, не до threshold) компресията се случва на
    порции, не на всяко съобщение — растежът между компресиите вече се вижда.
    """
    keep = keep if keep is not None else max(1, threshold - 20)
    conn = _init_db()
    cur = conn.execute("SELECT COUNT(*) FROM conversations;")
    total_messages = cur.fetchone()[0]

    if total_messages <= threshold:
        conn.close()
        return  # Няма нужда от обобщаване.

    # Избираме най-старите съобщения, които ще бъдат обобщени.
    # Тези, които остават след обобщението, са последните `keep`.
    cur = conn.execute(
        """
        SELECT id, role, content FROM conversations
        ORDER BY id ASC
        LIMIT ?;
        """,
        (total_messages - keep,),
    )
    old_messages = [{"id": row[0], "role": row[1], "content": row[2]} for row in cur.fetchall()]

    # Създаваме резюме.
    summary_text = _simple_summarize(old_messages)

    # Инсертваме ново системно съобщение след последно изтрито (за запазване на хронологията).
    # Изтриваме старите съобщения.
    ids_to_delete = tuple(msg["id"] for msg in old_messages)
    conn.execute(
        f"DELETE FROM conversations WHERE id IN ({','.join('?' * len(ids_to_delete))});",
        ids_to_delete,
    )
    # Инсертваме резюмето – използваме ролята "system".
    conn.execute(
        "INSERT INTO conversations (role, content) VALUES (?, ?);",
        ("system", summary_text),
    )
    conn.commit()
    conn.close()


def clear_session() -> None:
    """
    Изтрива цялата история от базата.
    """
    conn = _init_db()
    conn.execute("DROP TABLE IF EXISTS conversations;")
    conn.commit()
    conn.close()
    # Създаваме отново празна таблица.
    _init_db()


# --------------------------------------------------------------
# Пример за ръчно тестване (не се изпълнява при импорт)
# --------------------------------------------------------------
if __name__ == "__main__":
    # Чистим предишната сесия за демонстрация.
    clear_session()

    # Добавяме примерни съобщения.
    for i in range(55):
        role = "user" if i % 2 == 0 else "assistant"
        add_message(role, f"Message #{i+1} from {role}")

    # Взимаме последните 20 съобщения.
    recent = get_history(20)
    print("\n--- Последни 20 съобщения ---")
    for msg in recent:
        print(f"{msg['role']}: {msg['content']}")

    # Брой съобщения след автоматичното обобщение.
    conn_test = sqlite3.connect(DB_PATH)
    cur_test = conn_test.execute("SELECT COUNT(*) FROM conversations;")
    print("\nОбщ брой съобщения след обобщение:", cur_test.fetchone()[0])
    conn_test.close()
