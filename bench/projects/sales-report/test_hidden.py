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

def test_totals(tmp_path):
    totals, skipped = build_report(write(tmp_path, CSV))
    assert skipped == 3
    assert totals["Хляб"] == {"qty": 4, "revenue": 7.2}
    assert totals["Мляко"] == {"qty": 2, "revenue": 4.98}
    assert totals["Сирене"] == {"qty": 1, "revenue": 12.9}
    assert totals["Кафе"] == {"qty": 2, "revenue": 14.7}
    total = totals["ОБЩО"]  # условието („сумата на всички“) допуска число или речник
    revenue = total["revenue"] if isinstance(total, dict) else total
    assert revenue == pytest.approx(39.78)

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
