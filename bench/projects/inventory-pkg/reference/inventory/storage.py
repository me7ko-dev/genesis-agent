import sqlite3
from datetime import date
from decimal import Decimal

from .models import Item, Movement

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (sku TEXT PRIMARY KEY, name TEXT NOT NULL, unit TEXT NOT NULL,
                                  min_qty TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS movements (id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT NOT NULL,
                                      on_date TEXT NOT NULL, kind TEXT NOT NULL, qty TEXT NOT NULL,
                                      note TEXT NOT NULL);
"""


class Storage:
    def __init__(self, path):
        self.con = sqlite3.connect(path)
        self.con.executescript(SCHEMA)

    def item(self, sku):
        r = self.con.execute("SELECT sku, name, unit, min_qty FROM items WHERE sku = ?", (sku,)).fetchone()
        return Item(r[0], r[1], r[2], Decimal(r[3])) if r else None

    def items(self):
        return [Item(r[0], r[1], r[2], Decimal(r[3]))
                for r in self.con.execute("SELECT sku, name, unit, min_qty FROM items ORDER BY sku")]

    def save_item(self, it):
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?)",
                             (it.sku, it.name, it.unit, str(it.min_qty)))

    def add_movement(self, m):
        with self.con:
            self.con.execute("INSERT INTO movements (sku, on_date, kind, qty, note) VALUES (?, ?, ?, ?, ?)",
                             (m.sku, m.on.isoformat(), m.kind, str(m.qty), m.note))

    def movements(self, sku):
        return [Movement(sku, date.fromisoformat(d), k, Decimal(q), n) for d, k, q, n in self.con.execute(
            "SELECT on_date, kind, qty, note FROM movements WHERE sku = ? ORDER BY id", (sku,))]
