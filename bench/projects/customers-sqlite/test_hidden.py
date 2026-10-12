import sqlite3

from migrate import migrate

CSV = """Код,Име,Телефон,Имейл,Град,Регистриран
K001,Иван Петров,0888 123 456,ivan@abv.bg,София,05.03.2024
K002,"Георгиева, Мария",0888/123-456,,Пловдив,17.11.2023
K003,  Фирма „Слънце“ ЕООД ,02 987 6543,office@slance.bg, Варна ,01.01.2025
K004,Петър,+359 888 123456,,,29.02.2024
K005,Стоян,00359898765432,s@x.bg,Русе,10.10.2022
K006,Нина,няма,n@x.bg,Бургас,12.12.2021
,Без код,0888111222,,София,01.01.2024
K007,,0888111222,,София,01.01.2024
K008,Лоша Дата,0888111222,,София,31.02.2024
K009,Друга Лоша,0888111222,,София,2024-01-05

"""


def write(tmp_path, text, name="c.csv"):
    p = tmp_path / name
    p.write_bytes(text.encode("cp1251"))
    return str(p)


def rows(db):
    con = sqlite3.connect(db)
    try:
        return {r[0]: r[1:] for r in con.execute(
            "SELECT code, name, phone, email, city, registered FROM customers")}
    finally:
        con.close()


def test_first_import(tmp_path):
    db = str(tmp_path / "c.db")
    assert migrate(write(tmp_path, CSV), db) == {"inserted": 6, "updated": 0, "skipped": 4}
    got = rows(db)
    assert got["K001"] == ("Иван Петров", "+359888123456", "ivan@abv.bg", "София", "2024-03-05")
    assert got["K002"] == ("Георгиева, Мария", "+359888123456", None, "Пловдив", "2023-11-17")
    assert got["K003"] == ("Фирма „Слънце“ ЕООД", "+35929876543", "office@slance.bg", "Варна", "2025-01-01")
    assert got["K004"] == ("Петър", "+359888123456", None, None, "2024-02-29")
    assert got["K005"][1] == "+359898765432"
    assert got["K006"][1] is None
    assert set(got) == {"K001", "K002", "K003", "K004", "K005", "K006"}


def test_rerun_updates_instead_of_duplicating(tmp_path):
    db = str(tmp_path / "c.db")
    migrate(write(tmp_path, CSV), db)
    assert migrate(write(tmp_path, CSV), db) == {"inserted": 0, "updated": 6, "skipped": 4}
    changed = "Код,Име,Телефон,Имейл,Град,Регистриран\nK001,Иван Петров,0899 000 111,new@abv.bg,Перник,05.03.2024\nK100,Нов,,,,01.06.2026\n"
    assert migrate(write(tmp_path, changed, "d.csv"), db) == {"inserted": 1, "updated": 1, "skipped": 0}
    got = rows(db)
    assert got["K001"] == ("Иван Петров", "+359899000111", "new@abv.bg", "Перник", "2024-03-05")
    assert got["K100"] == ("Нов", None, None, None, "2026-06-01")
    assert len(got) == 7


def test_the_same_code_twice_in_one_file(tmp_path):
    db = str(tmp_path / "c.db")
    text = "Код,Име,Телефон,Имейл,Град,Регистриран\nK1,Стар,,,,01.01.2020\nK1,Нов,,,,02.02.2021\n"
    assert migrate(write(tmp_path, text), db) == {"inserted": 1, "updated": 1, "skipped": 0}
    assert rows(db)["K1"][0] == "Нов"


def test_the_table_schema(tmp_path):
    db = str(tmp_path / "c.db")
    migrate(write(tmp_path, CSV), db)
    con = sqlite3.connect(db)
    cols = {r[1]: (r[2].upper(), r[3], r[5]) for r in con.execute("PRAGMA table_info(customers)")}
    con.close()
    assert cols["code"][2] == 1                        # PRIMARY KEY
    assert cols["name"][1] == 1                        # NOT NULL
    assert set(cols) == {"code", "name", "phone", "email", "city", "registered"}


def test_cli(tmp_path):
    import os
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "migrate", write(tmp_path, CSV), str(tmp_path / "x.db")],
                       capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0, r.stderr
    for n in ("6", "0", "4"):
        assert n in r.stdout
    assert len(rows(str(tmp_path / "x.db"))) == 6
