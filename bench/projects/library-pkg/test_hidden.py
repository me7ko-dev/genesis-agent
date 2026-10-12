import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from library import Library, LimitReached, NotAvailable, NotFound

D = date(2026, 3, 2)
HP = "978-954-529-301-6"          # валидни ISBN-13
POD = "9789540903453"
TIME = "978 0 306 40615 7"
EXTRA = "9780131103627"


@pytest.fixture
def lib(tmp_path):
    return Library(str(tmp_path / "lib.db"))


def test_it_is_a_package_of_separate_modules():
    import library
    pkg = Path(library.__file__).parent
    for name in ("__init__", "models", "storage", "service", "errors", "cli"):
        assert (pkg / f"{name}.py").is_file(), name


@pytest.mark.parametrize("isbn", ["978-954-529-301-5", "97895452930", "abc", ""])
def test_invalid_isbn(lib, isbn):
    with pytest.raises(ValueError):
        lib.add_book(isbn, "Х", "У")


def test_borrow_and_return(lib):
    lib.add_book(HP, "Хари Потър", "Роулинг", copies=2)
    r = lib.add_reader("Ани")
    lib.borrow(r, HP, on=D)
    assert lib.available("9789545293016") == 1
    assert lib.return_book(r, HP, on=D + timedelta(days=14)) == 0
    assert lib.available(HP) == 2


def test_late_return(lib):
    lib.add_book(HP, "Хари Потър", "Роулинг")
    r = lib.add_reader("Ани")
    lib.borrow(r, HP, on=D)
    assert lib.return_book(r, HP, on=D + timedelta(days=20)) == 6


def test_the_same_isbn_adds_copies(lib):
    lib.add_book(POD, "Под игото", "Вазов")
    lib.add_book("978-954-090-345-3", "Под игото", "Вазов", copies=2)
    assert lib.available(POD) == 3


def test_rules(lib):
    for isbn in (HP, POD, TIME, EXTRA):
        lib.add_book(isbn, "Книга " + isbn[-4:], "Автор")
    a, b = lib.add_reader("Ани"), lib.add_reader("Боби")
    assert a != b
    lib.borrow(a, HP, on=D)
    with pytest.raises(NotAvailable):
        lib.borrow(b, HP, on=D)                 # няма свободна бройка
    with pytest.raises(NotAvailable):
        lib.borrow(a, HP, on=D)                 # същата книга два пъти
    lib.borrow(a, POD, on=D)
    lib.borrow(a, TIME, on=D)
    with pytest.raises(LimitReached):
        lib.borrow(a, EXTRA, on=D)
    assert lib.available(EXTRA) == 1
    with pytest.raises(NotFound):
        lib.borrow(999, EXTRA, on=D)
    with pytest.raises(NotFound):
        lib.borrow(b, "9780000000002", on=D)
    with pytest.raises(NotFound):
        lib.return_book(b, POD, on=D)           # Боби не я е взел
    with pytest.raises(NotFound):
        lib.available("9780000000002")


def test_overdue(lib):
    for isbn in (HP, POD, TIME):
        lib.add_book(isbn, "К", "А")
    a, b = lib.add_reader("Ани"), lib.add_reader("Боби")
    lib.borrow(a, HP, on=D)
    lib.borrow(b, POD, on=D - timedelta(days=10))
    lib.borrow(b, TIME, on=D + timedelta(days=5))
    today = D + timedelta(days=16)
    assert lib.overdue(today) == [(b, "9789540903453", 12), (a, "9789545293016", 2)]
    lib.return_book(b, POD, on=today)
    assert lib.overdue(today) == [(a, "9789545293016", 2)]


def test_survives_a_restart(tmp_path):
    db = str(tmp_path / "p.db")
    lib = Library(db)
    lib.add_book(HP, "Хари Потър", "Роулинг", copies=2)
    r = lib.add_reader("Ани")
    lib.borrow(r, HP, on=D)
    again = Library(db)
    assert again.available(HP) == 1
    assert again.overdue(D + timedelta(days=15)) == [(r, "9789545293016", 1)]


def test_cli_books(tmp_path):
    db = str(tmp_path / "c.db")
    lib = Library(db)
    lib.add_book(POD, "Под игото", "Иван Вазов", copies=3)
    lib.borrow(lib.add_reader("Ани"), POD, on=D)
    r = subprocess.run([sys.executable, "-m", "library.cli", "--db", db, "books"],
                       capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    assert r.returncode == 0, r.stderr
    line = next(ln for ln in r.stdout.splitlines() if "Под игото" in ln)
    assert "9789540903453" in line.replace("-", "") and "Иван Вазов" in line and "2/3" in line.replace(" ", "")
