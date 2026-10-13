from datetime import date
from decimal import Decimal

from .errors import InsufficientStock, NotFound
from .importer import read_rows
from .models import KINDS, Item, Movement, normalize_sku
from .quantities import parse_qty
from .storage import Storage


class Inventory:
    def __init__(self, db_path):
        self.db = Storage(db_path)

    def _item(self, sku):
        it = self.db.item(normalize_sku(sku))
        if it is None:
            raise NotFound(f"Няма стока {sku}")
        return it

    def _stock(self, sku):
        total = Decimal(0)
        for m in self.db.movements(sku):
            total += m.qty if m.kind == "in" else -m.qty
        return total

    def add_item(self, sku, name, unit="бр", min_qty=0):
        sku = normalize_sku(sku)
        name = str(name or "").strip()
        if not name:
            raise ValueError("Празно име на стока")
        min_qty = parse_qty(min_qty)
        if min_qty < 0:
            raise ValueError("Отрицателен минимум")
        if self.db.item(sku) is not None:
            raise ValueError(f"Стоката {sku} вече съществува")
        self.db.save_item(Item(sku, name, str(unit or "").strip() or "бр", min_qty))

    def stock(self, sku):
        return self._stock(self._item(sku).sku)

    def move(self, sku, kind, qty, on=None, note=""):
        if kind not in KINDS:
            raise ValueError(f"Видът е in или out, не {kind!r}")
        qty = parse_qty(qty)
        if qty <= 0:
            raise ValueError("Количеството трябва да е положително")
        it = self._item(sku)
        current = self._stock(it.sku)
        if kind == "out" and qty > current:
            raise InsufficientStock(f"Наличност {current}, искани {qty}")
        self.db.add_movement(Movement(it.sku, on or date.today(), kind, qty, note))
        return current + qty if kind == "in" else current - qty

    def history(self, sku):
        it = self._item(sku)
        return [{"date": m.on, "kind": m.kind, "qty": m.qty, "note": m.note} for m in self.db.movements(it.sku)]

    def low_stock(self):
        low = []
        for it in self.db.items():
            s = self._stock(it.sku)
            if s < it.min_qty:
                low.append((it.sku, it.name, s, it.min_qty))
        return sorted(low, key=lambda x: (-(x[3] - x[2]), x[0]))

    def import_csv(self, path, on=None):
        on = on or date.today()
        counts = {"added": 0, "updated": 0, "skipped": 0}
        for row in read_rows(path):
            if row is None:
                counts["skipped"] += 1
                continue
            sku, name, unit, qty, min_qty = row
            old = self.db.item(sku)
            self.db.save_item(Item(sku, name, unit, min_qty))
            diff = qty - (self._stock(sku) if old else Decimal(0))
            if diff:
                self.db.add_movement(Movement(sku, on, "in" if diff > 0 else "out", abs(diff), "импорт"))
            counts["updated" if old else "added"] += 1
        return counts
