import os
import re
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from inventory import InsufficientStock, Inventory, NotFound

D = date(2026, 3, 2)
NBSP = " "


@pytest.fixture
def inv(tmp_path):
    return Inventory(str(tmp_path / "inv.db"))


def write_csv(tmp_path, text, name="stock.csv"):
    p = tmp_path / name
    p.write_bytes(text.encode("cp1251"))
    return str(p)


def cli(db, *args):
    return subprocess.run([sys.executable, "-m", "inventory", "--db", db, *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"))


def test_it_is_a_package_of_separate_modules():
    import inventory
    pkg = Path(inventory.__file__).parent
    for name in ("__init__", "__main__", "models", "quantities", "storage", "service",
                 "importer", "errors", "cli"):
        assert (pkg / f"{name}.py").is_file(), name


def test_move_in_and_out_returns_decimal_stock(inv):
    inv.add_item("BOLT-8", "Болт М8", unit="бр", min_qty=10)
    assert inv.stock("BOLT-8") == 0
    got = inv.move("BOLT-8", "in", 25, on=D)
    assert got == Decimal(25) and isinstance(got, Decimal)
    assert inv.move("BOLT-8", "out", Decimal("7.5"), on=D) == Decimal("17.5")
    assert isinstance(inv.stock("BOLT-8"), Decimal) and inv.stock("BOLT-8") == Decimal("17.5")


@pytest.mark.parametrize("text,value", [
    ("1 234,50", Decimal("1234.5")), (f"1{NBSP}234,50", Decimal("1234.5")),
    ("0,25", Decimal("0.25")), (" 3,5 ", Decimal("3.5")),
])
def test_bulgarian_quantities_as_text(inv, text, value):
    inv.add_item("CEM", "Цимент", unit="кг")
    assert inv.move("CEM", "in", text, on=D) == value


def test_decimal_not_float(inv):
    inv.add_item("SAND", "Пясък", unit="кг")
    for _ in range(3):
        inv.move("SAND", "in", "0,1", on=D)
    assert inv.stock("SAND") == Decimal("0.3")
    inv.move("SAND", "out", "0,3", on=D)
    assert inv.stock("SAND") == 0


def test_sku_is_trimmed_and_upper_case(inv):
    inv.add_item(" ab-1 ", "Абразив")
    inv.move("ab-1", "in", 4, on=D)
    assert inv.stock("AB-1") == 4
    with pytest.raises(ValueError):
        inv.add_item("AB-1", "Пак той")


@pytest.mark.parametrize("args", [("", "Име"), ("  ", "Име"), ("X1", ""), ("X1", "  ")])
def test_add_item_rejects_empty_code_or_name(inv, args):
    with pytest.raises(ValueError):
        inv.add_item(*args)


def test_add_item_rejects_negative_minimum(inv):
    with pytest.raises(ValueError):
        inv.add_item("X1", "Нещо", min_qty=-1)


@pytest.mark.parametrize("kind,qty", [("in", 0), ("in", -5), ("out", "-1"), ("in", "abc"),
                                      ("in", "1,2,3"), ("sideways", 1)])
def test_invalid_movements(inv, kind, qty):
    inv.add_item("NUT", "Гайка")
    inv.move("NUT", "in", 5, on=D)
    with pytest.raises(ValueError):
        inv.move("NUT", kind, qty, on=D)
    assert inv.stock("NUT") == 5 and len(inv.history("NUT")) == 1


def test_unknown_item(inv):
    with pytest.raises(NotFound):
        inv.stock("NOPE")
    with pytest.raises(NotFound):
        inv.move("NOPE", "in", 1, on=D)
    with pytest.raises(NotFound):
        inv.history("NOPE")


def test_insufficient_stock_changes_nothing(inv):
    inv.add_item("PIPE", "Тръба", unit="м")
    inv.move("PIPE", "in", "2,5", on=D)
    with pytest.raises(InsufficientStock):
        inv.move("PIPE", "out", "2,51", on=D)
    assert inv.stock("PIPE") == Decimal("2.5")
    assert len(inv.history("PIPE")) == 1
    assert inv.move("PIPE", "out", "2,5", on=D) == 0          # точно колкото има — може


def test_history(inv):
    inv.add_item("GLUE", "Лепило")
    inv.move("GLUE", "in", 10, on=date(2026, 3, 1), note="доставка")
    inv.move("GLUE", "out", "1,5", on=date(2026, 2, 27))
    h = inv.history("glue")
    assert [(m["date"], m["kind"], m["qty"], m["note"]) for m in h] == [
        (date(2026, 3, 1), "in", Decimal(10), "доставка"),
        (date(2026, 2, 27), "out", Decimal("1.5"), ""),
    ]


def test_low_stock(inv):
    inv.add_item("A", "Първа", min_qty=10)
    inv.add_item("B", "Втора", min_qty=5)
    inv.add_item("C", "Трета", min_qty=3)
    inv.add_item("D", "Четвърта", min_qty="2,5")
    inv.add_item("E", "Без минимум")
    inv.move("A", "in", 7, on=D)           # недостиг 3
    inv.move("B", "in", 1, on=D)           # недостиг 4
    inv.move("C", "in", 3, on=D)           # точно на минимума — не е под него
    #  D: 0 при минимум 2,5 → недостиг 2,5; E: 0 при минимум 0 — не е под него
    inv.add_item("AA", "Още една", min_qty=4)
    inv.move("AA", "in", 1, on=D)          # недостиг 3, като A — по код
    assert inv.low_stock() == [
        ("B", "Втора", Decimal(1), Decimal(5)),
        ("A", "Първа", Decimal(7), Decimal(10)),
        ("AA", "Още една", Decimal(1), Decimal(4)),
        ("D", "Четвърта", Decimal(0), Decimal("2.5")),
    ]


def test_survives_a_restart(tmp_path):
    db = str(tmp_path / "p.db")
    first = Inventory(db)
    first.add_item("OIL", "Масло", unit="л", min_qty=20)
    first.move("OIL", "in", "12,75", on=D)
    again = Inventory(db)
    assert again.stock("OIL") == Decimal("12.75")
    assert again.low_stock() == [("OIL", "Масло", Decimal("12.75"), Decimal(20))]


CSV = (
    "Код;Наименование;Мярка;Количество;Минимум\n"
    "w-01;Винт 4x40;бр;1 234;100\n"
    f"W-02;Шайба М6;;2{NBSP}500,5;\n"
    "K-10;Кабел ПВ-А1 2,5 мм2;м;0,5;10\n"
    "E-1;Празен;кг;;\n"
    ";Без код;бр;5;1\n"
    "N-1;;бр;5;1\n"
    "BAD-1;Лошо число;бр;5 бр.;1\n"
    "BAD-2;Отрицателно;бр;-3;1\n"
    "\n"
)


def test_import_new_items(inv, tmp_path):
    assert inv.import_csv(write_csv(tmp_path, CSV), on=D) == {"added": 4, "updated": 0, "skipped": 4}
    assert inv.stock("W-01") == 1234
    assert inv.stock("W-02") == Decimal("2500.5")
    assert inv.stock("K-10") == Decimal("0.5")
    assert inv.stock("E-1") == 0 and inv.history("E-1") == []
    assert [(m["date"], m["kind"], m["qty"], m["note"]) for m in inv.history("W-01")] == [
        (D, "in", Decimal(1234), "импорт")]
    assert inv.low_stock() == [("K-10", "Кабел ПВ-А1 2,5 мм2", Decimal("0.5"), Decimal(10))]
    for sku in ("BAD-1", "BAD-2", "N-1"):
        with pytest.raises(NotFound):
            inv.stock(sku)


def test_import_brings_existing_stock_to_the_file(inv, tmp_path):
    inv.add_item("W-01", "Стар винт", min_qty=1)
    inv.move("W-01", "in", 2000, on=D)
    inv.add_item("K-10", "Кабел")
    inv.move("K-10", "in", "0,5", on=D)
    later = date(2026, 3, 9)
    assert inv.import_csv(write_csv(tmp_path, CSV), on=later) == {"added": 2, "updated": 2, "skipped": 4}
    assert inv.stock("W-01") == 1234
    assert inv.history("W-01")[-1] == {"date": later, "kind": "out", "qty": Decimal(766), "note": "импорт"}
    assert len(inv.history("K-10")) == 1                       # без разлика — без движение
    assert ("W-01", "Винт 4x40", Decimal(1234), Decimal(100)) not in inv.low_stock()
    assert inv.low_stock()[0][:2] == ("K-10", "Кабел ПВ-А1 2,5 мм2")


def test_import_twice_adds_no_movements(inv, tmp_path):
    path = write_csv(tmp_path, CSV)
    inv.import_csv(path, on=D)
    assert inv.import_csv(path, on=D) == {"added": 0, "updated": 4, "skipped": 4}
    assert inv.stock("W-02") == Decimal("2500.5") and len(inv.history("W-02")) == 1


def test_cli_add_move_and_errors(tmp_path):
    db = str(tmp_path / "c.db")
    r = cli(db, "add", "cem-25", "Цимент 25 кг", "--unit", "чувал", "--min", "4")
    assert r.returncode == 0, r.stderr
    r = cli(db, "move", "CEM-25", "in", "10", "--date", "2026-03-02")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1].strip() == "10"
    r = cli(db, "move", "CEM-25", "out", "7,5")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1].strip() == "2,5"
    assert Inventory(db).history("CEM-25")[0]["date"] == D
    r = cli(db, "move", "CEM-25", "out", "3")
    assert r.returncode == 1 and r.stderr.strip() and "Traceback" not in r.stderr
    r = cli(db, "move", "NOPE", "in", "3")
    assert r.returncode == 1 and r.stderr.strip() and "Traceback" not in r.stderr
    r = cli(db, "move", "CEM-25", "in", "много")
    assert r.returncode == 1 and r.stderr.strip() and "Traceback" not in r.stderr
    assert Inventory(db).stock("CEM-25") == Decimal("2.5")


def test_cli_report(tmp_path):
    db = str(tmp_path / "r.db")
    inv = Inventory(db)
    inv.add_item("CEM-25", "Цимент 25 кг", min_qty="4,000")
    inv.move("CEM-25", "in", "2,500", on=D)
    inv.add_item("FULL", "Пълна стока", min_qty=1)
    inv.move("FULL", "in", 50, on=D)
    r = cli(db, "report")
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    hit = [ln for ln in lines if "CEM-25" in ln]
    assert len(hit) == 1 and "Цимент 25 кг" in hit[0]
    numbers = re.findall(r"(?<![\d,.])\d+(?:,\d+)?(?![\d,.])", hit[0].replace("CEM-25", "").replace("25 кг", ""))
    assert numbers == ["2,5", "4"], hit[0]
    assert not any("FULL" in ln for ln in lines)


def test_cli_import(tmp_path):
    db = str(tmp_path / "i.db")
    r = cli(db, "import", write_csv(tmp_path, CSV))
    assert r.returncode == 0, r.stderr
    assert re.search(r"добавени:\s*4,\s*обновени:\s*0,\s*пропуснати:\s*4", r.stdout), r.stdout
    assert Inventory(db).stock("W-02") == Decimal("2500.5")
