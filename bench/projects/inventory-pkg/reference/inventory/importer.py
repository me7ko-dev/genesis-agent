import csv

from .models import normalize_sku
from .quantities import parse_qty


def _row(cells):
    """(sku, име, мярка, количество, минимум) или None — редът се пропуска."""
    cells = [c.strip() for c in cells] + [""] * 5
    code, name, unit, qty, min_qty = cells[:5]
    if not code or not name:
        return None
    try:
        q = parse_qty(qty or "0")
        m = parse_qty(min_qty or "0")
    except ValueError:
        return None
    if q < 0 or m < 0:
        return None
    return normalize_sku(code), name, unit or "бр", q, m


def read_rows(path):
    with open(path, encoding="cp1251", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        next(reader, None)                     # заглавният ред
        for cells in reader:
            if any(c.strip() for c in cells):
                yield _row(cells)
