import pytest
from report import build_report

CSV = """дата;продукт;количество;цена
01.09.2026;Хляб;3;1,80
01.09.2026;Мляко;2;2,49
02.09.2026;Хляб;1;1,80
02.09.2026;Сирене;1;12,90
02.09.2026;Мляко
03.09.2026;Кафе;две;5,00
03.09.2026;Кафе;2;abc
03.09.2026;Кафе;2;7,35
"""

def write(tmp_path, text, enc="utf-8"):
    p = tmp_path / "s.csv"
    p.write_text(text, encoding=enc)
    return str(p)

def same(row, qty, revenue):
    """Условието не казва тип: Decimal('7.20') и 7.2 са един и същ отговор (2026-10-02)."""
    return (set(row) == {"qty", "revenue"} and float(row["qty"]) == qty
            and float(row["revenue"]) == pytest.approx(revenue, abs=1e-9))

def test_totals(tmp_path):
    totals, skipped = build_report(write(tmp_path, CSV))
    assert skipped == 3
    assert same(totals["Хляб"], 4, 7.2)
    assert same(totals["Мляко"], 2, 4.98)
    assert same(totals["Сирене"], 1, 12.9)
    assert same(totals["Кафе"], 2, 14.7)
    total = totals["ОБЩО"]  # условието („сумата на всички“) допуска число или речник
    revenue = total["revenue"] if isinstance(total, dict) else total
    assert float(revenue) == pytest.approx(39.78)

def test_excel_bom(tmp_path):
    """Excel „CSV UTF-8“ пише BOM — заглавието не бива да стане продукт/грешка."""
    totals, skipped = build_report(write(tmp_path, CSV, enc="utf-8-sig"))
    assert skipped == 3
    assert totals["Хляб"]["qty"] == 4

def test_only_header(tmp_path):
    totals, skipped = build_report(write(tmp_path, "дата;продукт;количество;цена\n"))
    assert skipped == 0
    total = totals.get("ОБЩО", 0)
    assert (total["revenue"] if isinstance(total, dict) else total) == 0
