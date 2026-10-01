import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sklad
from sklad import InsufficientStock, Warehouse


@pytest.fixture
def w(tmp_path):
    return Warehouse(str(tmp_path / "s.json"))


def test_package_has_modules():
    pkg = Path(sklad.__file__).parent
    modules = [p for p in pkg.glob("*.py") if not p.name.startswith("test")]
    assert len(modules) >= 5, [p.name for p in modules]
    assert (pkg / "cli.py").is_file()


def test_fifo(w):
    w.receive("A", 10, 2.00, "2026-09-01")
    w.receive("A", 5, 3.00, "2026-09-03")
    w.receive("B", 4, 1.25, "2026-09-02")
    assert w.sell("A", 12, 5.00, "2026-09-10") == pytest.approx(26.00)  # 10×2 + 2×3
    assert w.stock("A") == 3
    assert w.stock("B") == 4
    assert w.stock_value() == pytest.approx(14.00)  # 3×3 + 4×1.25
    assert w.sell("A", 3, 5.00, "2026-09-11") == pytest.approx(9.00)
    assert w.stock("A") == 0
    assert w.stock("НЯМА") == 0


def test_insufficient_changes_nothing(w, tmp_path):
    w.receive("A", 3, 2.00, "2026-09-01")
    w.sell("A", 1, 4.00, "2026-09-02")
    before = (tmp_path / "s.json").read_text("utf-8")
    with pytest.raises(InsufficientStock):
        w.sell("A", 3, 4.00, "2026-09-03")
    with pytest.raises(InsufficientStock):
        w.sell("B", 1, 4.00, "2026-09-03")
    assert w.stock("A") == 2
    assert w.stock_value() == pytest.approx(4.00)
    assert w.month_report("2026-09") == {"revenue": 4.00, "cogs": 2.00, "profit": 2.00}
    assert (tmp_path / "s.json").read_text("utf-8") == before
    assert w.sell("A", 2, 4.00, "2026-09-04") == pytest.approx(4.00)


@pytest.mark.parametrize("args", [
    ("A", 0, 1.0, "2026-09-01"), ("A", -2, 1.0, "2026-09-01"), ("A", 2.5, 1.0, "2026-09-01"),
    ("A", 2, 0, "2026-09-01"), ("A", 2, -1, "2026-09-01"), ("A", 2, 1.0, "01.09.2026"),
    ("A", 2, 1.0, "2026-02-30"),
])
def test_receive_rejects(w, args):
    with pytest.raises(ValueError):
        w.receive(*args)
    assert w.stock("A") == 0


def test_persists_partly_sold_lots(tmp_path):
    path = str(tmp_path / "p.json")
    w1 = Warehouse(path)
    w1.receive("A", 4, 1.10, "2026-09-01")
    w1.receive("A", 4, 1.30, "2026-09-02")
    w1.sell("A", 3, 2.00, "2026-09-05")
    json.loads(Path(path).read_text("utf-8"))  # it really is JSON
    w2 = Warehouse(path)
    assert w2.stock("A") == 5
    assert w2.sell("A", 2, 2.00, "2026-10-01") == pytest.approx(2.40)  # 1×1.10 + 1×1.30
    assert w2.stock_value() == pytest.approx(3.90)


def test_month_report_and_rounding(w):
    for _ in range(3):
        w.receive("K", 1, 0.10, "2026-09-01")
    w.receive("K", 10, 1.00, "2026-09-01")
    w.sell("K", 3, 0.35, "2026-09-30")
    w.sell("K", 2, 2.50, "2026-10-01")
    sep = w.month_report("2026-09")
    assert sep == {"revenue": 1.05, "cogs": 0.30, "profit": 0.75}
    assert w.month_report("2026-10") == {"revenue": 5.00, "cogs": 2.00, "profit": 3.00}
    assert w.month_report("2026-11") == {"revenue": 0.0, "cogs": 0.0, "profit": 0.0}
    assert all(isinstance(v, float) for v in sep.values())


def test_cli(tmp_path):
    db = str(tmp_path / "cli.json")
    env = dict(os.environ, PYTHONUTF8="1")

    def cli(*args):
        r = subprocess.run([sys.executable, "-m", "sklad.cli", "--db", db, *args], env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, check=False)
        assert r.returncode == 0, r.stderr
        return r.stdout

    cli("receive", "MLYAKO", "10", "1.20", "2026-09-01")
    cli("sell", "MLYAKO", "4", "2.00", "2026-09-02")
    assert cli("stock", "MLYAKO").strip() == "6"
    report = cli("report", "2026-09")
    assert "8" in report and "4.8" in report.replace(",", ".")
    assert Warehouse(db).stock("MLYAKO") == 6
