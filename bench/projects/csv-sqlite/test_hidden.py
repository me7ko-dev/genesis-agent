import sqlite3

from migrate import migrate

HEADER = "ЕИК;Име;Град;Оборот;От дата\n"
CSV = HEADER + (
    '000123456;"Иванов и син; ООД";София;1 234,50;05.03.2019\n'   # ; в кавички, водеща нула
    "204512377;Зелен свят ЕООД;Пловдив;12 500,00;17.11.2021\n"  # Excel: NBSP за хилядите
    "131468980;Мега АД;Варна;980,00;01.01.2020\n"
    "175074752;Счупена дата ООД;Русе;100,00;31.02.2022\n"
    "200000000;Текст ООД;Бургас;много;01.02.2023\n"
    "121212121;Къс ред\n"
)
ROWS = [
    ("000123456", "Иванов и син; ООД", "София", 1234.5, "2019-03-05"),
    ("131468980", "Мега АД", "Варна", 980.0, "2020-01-01"),
    ("204512377", "Зелен свят ЕООД", "Пловдив", 12500.0, "2021-11-17"),
]


def write(tmp_path, text, name="c.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8-sig")  # Excel „CSV UTF-8“ пише BOM
    return str(p)


def rows(db):
    with sqlite3.connect(db) as con:
        return con.execute("SELECT eik, name, city, turnover, since FROM clients ORDER BY eik").fetchall()


def test_rows_types_and_skipped(tmp_path):
    db = str(tmp_path / "c.db")
    assert migrate(write(tmp_path, CSV), db) == (3, 3)
    assert rows(db) == ROWS
    with sqlite3.connect(db) as con:
        types = con.execute("SELECT DISTINCT typeof(eik), typeof(turnover) FROM clients").fetchall()
    assert types == [("text", "real")]


def test_monthly_rerun_updates_not_duplicates(tmp_path):
    db = str(tmp_path / "c.db")
    migrate(write(tmp_path, CSV), db)
    assert migrate(write(tmp_path, CSV), db) == (3, 3)
    assert rows(db) == ROWS
    october = HEADER + "204512377;Зелен свят ЕООД;Асеновград;13 000,10;17.11.2021\n"
    assert migrate(write(tmp_path, october, "oct.csv"), db) == (1, 0)
    assert rows(db)[2] == ("204512377", "Зелен свят ЕООД", "Асеновград", 13000.1, "2021-11-17")
    assert len(rows(db)) == 3


def test_only_header(tmp_path):
    db = str(tmp_path / "c.db")
    assert migrate(write(tmp_path, HEADER), db) == (0, 0)
    assert rows(db) == []
