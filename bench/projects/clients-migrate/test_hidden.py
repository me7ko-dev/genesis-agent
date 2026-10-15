import sqlite3
import subprocess
import sys

import migrate as m

HEADER = "Име;Имейл;Телефон;Дата на регистрация;Град\n"


def write_csv(path, rows):
    # Excel's "CSV UTF-8": a BOM in front of the header
    path.write_text(HEADER + "".join(r + "\n" for r in rows), encoding="utf-8-sig")
    return str(path)


def clients(db):
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT name, email, phone, registered, city FROM clients ORDER BY email").fetchall()
    finally:
        con.close()


ROWS = [
    "Иван Петров;Ivan.Petrov@Abv.bg;0888 123 456;05.03.2021;София",
    " Мария Иванова ; maria@gmail.com ;+359 877 11 22 33;29.02.2024; Пловдив ",
    "Без Имейл;;0888000000;01.01.2022;Варна",
    "Лоша Дата;bad@date.bg;0888111222;31.02.2025;Бургас",
    "Георги Георгиев;georgi@mail.bg;00359898765432;15.11.2019;Русе",
    "Иван П.;ivan.petrov@abv.bg;0888-123-457;06.03.2021;София",
    "Петя Без Телефон;petya@abv.bg;;10.10.2010;Шумен",
]


def test_first_run(tmp_path):
    db = str(tmp_path / "c.db")
    stats = m.migrate(write_csv(tmp_path / "a.csv", ROWS), db)
    assert stats == {"inserted": 4, "updated": 1, "skipped": 2}
    assert clients(db) == [
        ("Георги Георгиев", "georgi@mail.bg", "+359898765432", "2019-11-15", "Русе"),
        ("Иван П.", "ivan.petrov@abv.bg", "+359888123457", "2021-03-06", "София"),
        ("Мария Иванова", "maria@gmail.com", "+359877112233", "2024-02-29", "Пловдив"),
        ("Петя Без Телефон", "petya@abv.bg", None, "2010-10-10", "Шумен"),
    ]


def test_second_run_updates_without_duplicates(tmp_path):
    db = str(tmp_path / "c.db")
    m.migrate(write_csv(tmp_path / "a.csv", ROWS), db)
    again = m.migrate(write_csv(tmp_path / "a.csv", ROWS), db)
    assert again == {"inserted": 0, "updated": 5, "skipped": 2}
    assert len(clients(db)) == 4

    newer = write_csv(tmp_path / "b.csv", [
        "Мария Иванова-Колева;MARIA@gmail.com;0877 99 88 77;29.02.2024;Стара Загора",
        "Нов Клиент;new@client.bg;02 981 23 45;01.10.2026;София",
    ])
    assert m.migrate(newer, db) == {"inserted": 1, "updated": 1, "skipped": 0}
    rows = {r[1]: r for r in clients(db)}
    assert len(rows) == 5
    assert rows["maria@gmail.com"] == (
        "Мария Иванова-Колева", "maria@gmail.com", "+359877998877", "2024-02-29", "Стара Загора")
    assert rows["new@client.bg"][2] == "+35929812345"


def test_email_is_unique_in_the_schema(tmp_path):
    db = str(tmp_path / "c.db")
    m.migrate(write_csv(tmp_path / "a.csv", ROWS[:1]), db)
    con = sqlite3.connect(db)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(clients)")]
        assert cols[:6] == ["id", "name", "email", "phone", "registered", "city"]
        try:
            con.execute("INSERT INTO clients (name, email) VALUES ('x', 'ivan.petrov@abv.bg')")
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("email is not UNIQUE")
    finally:
        con.close()


def test_command_line(tmp_path):
    csv_path = write_csv(tmp_path / "a.csv", ROWS)
    db = str(tmp_path / "cli.db")
    r = subprocess.run([sys.executable, m.__file__, csv_path, db], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    assert len(clients(db)) == 4
    for n in ("4", "1", "2"):
        assert n in r.stdout
