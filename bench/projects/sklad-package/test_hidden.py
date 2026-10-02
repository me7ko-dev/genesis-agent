import json
import subprocess
import sys
from decimal import Decimal

import pytest
from sklad import report, service, storage
from sklad.models import Product


def stock():
    products = {}
    service.receive(products, "A1", "Болт M6", 10, Decimal("0.10"))
    service.receive(products, "B2", "Гайка M6", 3, Decimal("0.20"))
    service.receive(products, "C3", "Шайба", 5, Decimal("0.05"))
    return products


def test_storage_keeps_the_price_exact_and_a_missing_file_is_empty(tmp_path):
    db = tmp_path / "s.json"
    assert storage.load(db) == {}
    products = {"X": Product("X", "Кабел", 2, Decimal("12.50"))}
    storage.save(db, products)
    assert "12.50" in db.read_text(encoding="utf-8")
    assert storage.load(db) == products
    json.loads(db.read_text(encoding="utf-8"))  # истински JSON


def test_receive_adds_to_an_existing_product():
    products = stock()
    service.receive(products, "A1", "Болт M6", 5, Decimal("0.10"))
    assert products["A1"].qty == 15


def test_issue_without_enough_changes_nothing():
    products = stock()
    with pytest.raises(ValueError):
        service.issue(products, "B2", 4)
    with pytest.raises(ValueError):
        service.issue(products, "NOPE", 1)
    assert products["B2"].qty == 3
    service.issue(products, "B2", 3)
    assert products["B2"].qty == 0


def test_low_stock_is_strictly_below_and_sorted():
    products = stock()
    assert service.low_stock(products, 5) == ["B2"]
    assert service.low_stock(products, 11) == ["A1", "B2", "C3"]


def test_total_value_is_an_exact_decimal():
    products = stock()
    assert report.total_value(products) == Decimal("1.85")  # 1.00 + 0.60 + 0.25
    assert isinstance(report.total_value(products), Decimal)


def _cli(tmp_path, *args):
    return subprocess.run([sys.executable, "-m", "sklad", "--db", str(tmp_path / "s.json"), *args],
                          cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False)


def test_the_command_line_end_to_end(tmp_path):
    assert _cli(tmp_path, "receive", "A1", "Болт", "10", "0.10").returncode == 0
    assert _cli(tmp_path, "receive", "B2", "Гайка", "3", "0.20").returncode == 0
    assert _cli(tmp_path, "issue", "A1", "3").returncode == 0
    out = _cli(tmp_path, "report")
    assert out.returncode == 0
    total = next(ln for ln in out.stdout.splitlines() if "ОБЩО" in ln)
    assert "1.30" in total.replace(",", ".")  # 7 × 0.10 + 3 × 0.20
    bad = _cli(tmp_path, "issue", "B2", "99")
    assert bad.returncode == 1 and "Traceback" not in bad.stdout + bad.stderr
    assert (bad.stdout + bad.stderr).strip()
