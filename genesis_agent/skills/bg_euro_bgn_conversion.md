---
name: bg_euro_bgn_conversion
category: domain
description: Еврото в България — официална валута от 1 януари 2026, фиксиран курс
  1 EUR = 1.95583 BGN, превръщане лева↔евро със закръгляне до цент, ключови дати.
triggers:
- евро
- лева в евро
- левове евро курс
- 1.95583
- превърни лв в евро
- еврото в българия
- bgn eur conversion bulgaria euro adoption
version: '1.0'
author: Genesis
last_updated: '2026-09-25T12:00:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-09-25: Genesis 2/2 пъти сложи
грешна дата на еврото (2025-01-01 и 2023-01-01), макар курсът да беше верен.
Източници: trade.ec.europa.eu („България приема еврото от 1 януари 2026 г.“),
evroto.bg, БНБ.

## Python Код
```python
"""Еврото в България.

Факти:
- От 1 януари 2026 официалната валута е еврото (EUR); до 31 декември 2025 — левът (BGN).
- Фиксиран и неотменим курс: 1 EUR = 1.95583 BGN.
- Двойно обращение (плащане и в левове, и в евро): 1–31 януари 2026.
- Двойно обозначаване на цените (лв и €): задължително 8 август 2025 – 8 август 2026.
- Обмяна левове → евро без такса в банките и „Български пощи“: до 30 юни 2026.
Превръщането е по курса, закръглено до цент (ROUND_HALF_UP); не се ползва
обратен курс като 0.51129 — точността се губи.
"""
from __future__ import annotations

import datetime
from decimal import ROUND_HALF_UP, Decimal

BGN_PER_EUR = Decimal("1.95583")
EURO_ADOPTION = datetime.date(2026, 1, 1)
DUAL_CIRCULATION_END = datetime.date(2026, 1, 31)
DUAL_PRICING = (datetime.date(2025, 8, 8), datetime.date(2026, 8, 8))


def bgn_to_eur(amount_bgn: float | str | Decimal) -> Decimal:
    return (Decimal(str(amount_bgn)) / BGN_PER_EUR).quantize(Decimal("0.01"), ROUND_HALF_UP)


def eur_to_bgn(amount_eur: float | str | Decimal) -> Decimal:
    return (Decimal(str(amount_eur)) * BGN_PER_EUR).quantize(Decimal("0.01"), ROUND_HALF_UP)


def official_currency(day: datetime.date) -> str:
    """'EUR' от 2026-01-01 нататък, иначе 'BGN'."""
    return "EUR" if day >= EURO_ADOPTION else "BGN"


if __name__ == "__main__":
    assert bgn_to_eur("1.95583") == Decimal("1.00")
    assert bgn_to_eur(100) == Decimal("51.13")          # 100 / 1.95583 = 51.1292…
    assert eur_to_bgn(10) == Decimal("19.56")           # 19.5583
    assert official_currency(datetime.date(2025, 12, 31)) == "BGN"
    assert official_currency(datetime.date(2026, 1, 1)) == "EUR"
    print("OK")
```
