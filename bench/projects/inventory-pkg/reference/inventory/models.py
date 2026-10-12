from dataclasses import dataclass
from datetime import date
from decimal import Decimal

KINDS = ("in", "out")


@dataclass
class Item:
    sku: str
    name: str
    unit: str
    min_qty: Decimal


@dataclass
class Movement:
    sku: str
    on: date
    kind: str
    qty: Decimal
    note: str


def normalize_sku(sku):
    s = str(sku or "").strip().upper()
    if not s:
        raise ValueError("Празен код на стока")
    return s
