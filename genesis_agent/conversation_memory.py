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
# Граници на резюмето. То се праща при ВСЯКА заявка до края на сесията,
# затова е ограничено — но 300 знака от слепен текст се оказаха твърде малко,
# за да остане каквото и да е (виж мерката в docstring-а отдолу).
_SUMMARY_ITEM_CHARS = 120
_SUMMARY_MAX_ITEMS = 8


def _simple_summarize(messages: list[dict[str, str]]) -> str:
    """Детерминистично резюме на блок съобщения — без LLM повикване.

    Старата версия слепваше ВСИЧКО и взимаше първите 300 знака. Измерено върху
    реалната база на оператора, 21 съобщения се свиха до това:

        [Context summary] 21 messages from roles: assistant, user |
        Error: цялата верига е изчерпана | последна: skip: no HF_TOKEN
        configured  (×4, до изчерпване на знаците)

    Тоест цялото резюме беше едно и също съобщение за грешка, повторено
    четири пъти, а всяка реплика на ЧОВЕКА от този блок изчезна. Това е
    паметта, която се праща наново при всяка заявка до края на сесията:
    плаща се за нея, а не носи нищо.

    Затова сега: репликите на човека водят (те носят НАМЕРЕНИЕТО — какво е
    поискано; отговорите на модела се извеждат от тях), повторенията отпадат,
    и всяка реплика влиза съкратена. Ако не се събират, остават първата (с
    какво започнахме) и последните (докъде стигнахме) — и двата края са
    по-полезни от произволен отрязък от средата.
    """
    roles = {msg.get("role", "?") for msg in messages}
    header = (f"[Context summary] {len(messages)} messages from roles: "
              + ", ".join(sorted(roles)))

    ordered = ([m for m in messages if m.get("role") == "user"]
               + [m for m in messages if m.get("role") != "user"])
    items: list[str] = []
    seen: set[str] = set()
    for msg in ordered:
        text = " ".join((msg.get("content") or "").split())[:_SUMMARY_ITEM_CHARS]
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        items.append(f"{msg.get('role', '?')}: {text}")

    if len(items) > _SUMMARY_MAX_ITEMS:
        items = [items[0], "…"] + items[-(_SUMMARY_MAX_ITEMS - 1):]
    return header + (" | " + " | ".join(items) if items else "")

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


def get_history(last_n: int = 20) -> list[dict[str, str]]:
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

    # Колко от най-старите ще обобщим. Проверката е задължителна, защото
    # `keep` е публичен параметър (виж docstring-а на модула) и при
    # `keep >= total_messages` изразът излиза нула или отрицателен — два
    # отделни начина да се счупи (bug fix, 2026-08-12):
    #   • LIMIT 0  → празен резултат → `DELETE ... WHERE id IN ()`, което не е
    #     валиден SQL и гърми с OperationalError;
    #   • LIMIT <0 → SQLite го чете като БЕЗ ограничение, тоест избира и трие
    #     ЦЕЛИЯ разговор — точната противоположност на "запази последните N".
    to_summarize = total_messages - max(0, keep)
    if to_summarize <= 0:
        conn.close()
        return

    # Избираме най-старите съобщения, които ще бъдат обобщени.
    # Тези, които остават след обобщението, са последните `keep`.
    cur = conn.execute(
        """
        SELECT id, role, content FROM conversations
        ORDER BY id ASC
        LIMIT ?;
        """,
        (to_summarize,),
    )
    old_messages = [{"id": row[0], "role": row[1], "content": row[2]} for row in cur.fetchall()]

    # Създаваме резюме.
    summary_text = _simple_summarize(old_messages)

    # Изтриваме старите съобщения; мястото им се заема от резюмето отдолу.
    ids_to_delete = tuple(msg["id"] for msg in old_messages)
    conn.execute(
        f"DELETE FROM conversations WHERE id IN ({','.join('?' * len(ids_to_delete))});",
        ids_to_delete,
    )
    # Резюмето заема МЯСТОТО на обобщения блок, не опашката на разговора.
    # Редът по-горе твърдеше точно това („след последно изтрито, за запазване
    # на хронологията"), но вмъкваше без id — а AUTOINCREMENT дава следващото
    # СВОБОДНО, тоест най-голямото. Резултатът: резюме на НАЙ-СТАРИТЕ съобщения
    # се нареждаше като НАЙ-НОВОТО (`get_history` сортира по id). Моделът
    # виждаше „[Context summary] 21 messages…" след последния въпрос на човека,
    # тоест разговорът му се поднасяше разбъркан точно в момента, в който вече
    # е достатъчно дълъг, за да има значение. Най-малкото изтрито id е точно
    # позицията на блока и е свободно след DELETE-а отгоре.
    conn.execute(
        "INSERT INTO conversations (id, role, content) VALUES (?, ?, ?);",
        (min(ids_to_delete), "system", summary_text),
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
