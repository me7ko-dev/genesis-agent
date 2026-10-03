---
name: money_round_up_step
category: domain
min_score: 3
description: Цена, закръглена НАГОРЕ до стъпка (0,05 / 0,10 / 1) — с Decimal от самия текст, не с
  math.ceil върху float (26,25 × 1,1 × 1,2 = 34,65 точно, а float дава 34.650000000000006 → 34,70).
triggers:
- закръглена нагоре до стъпката
- закръгляне нагоре цена стъпка
- round up price to step
version: '1.0'
author: Genesis
last_updated: '2026-10-04T02:50:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-10-02 (bench cli-config, пуск 1):
`math.ceil(value / step) * step` върху float — 26,25 с надценка 10 % и ДДС
20 % е точно 34,65, но float дава 34.650000000000006 и „нагоре“ става 34,70.
`Decimal(str(34.650000000000006))` пази шума — Decimal трябва още от входа.

## Python Код
```python
"""Цена → закръглена НАГОРЕ до стъпка, без шума на float.

Факти:
- 26.25 * 1.1 * 1.2 == 34.650000000000006 във float. math.ceil(x / 0.05) * 0.05 → 34.7.
- Decimal(str(float_result)) НЕ помага — шумът вече е в числото. Направи Decimal от
  ТЕКСТА на входа ("26,25" → Decimal("26.25")), процентите като Decimal(процент) / 100,
  и смятай всичко в Decimal.
- Нагоре до стъпка: (value / step).to_integral_value(rounding=ROUND_CEILING) * step,
  после .quantize(Decimal("0.01")). Точно кратно (34,65 при стъпка 0,05) остава същото.
- Изход с десетична запетая: f"{d:.2f}".replace(".", ",") — "34,65".
- Стъпката от JSON/CLI е float (0.05) → Decimal(str(step)), не Decimal(step)
  (Decimal(0.05) = 0.05000000000000000277…).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal


def to_decimal(text: str) -> Decimal:
    """'26,25' / '1 234,50' / '26.25' → Decimal; без float по пътя."""
    return Decimal("".join(str(text).split()).replace(",", "."))


def round_up(value: Decimal, step: Decimal) -> Decimal:
    """Нагоре до кратно на step; точно кратно не мърда."""
    return ((value / step).to_integral_value(rounding=ROUND_CEILING) * step).quantize(Decimal("0.01"))


def sale_price(cost: str, markup_pct: float | int | str, vat_pct: float | int | str,
               step: float | str = "0.05") -> Decimal:
    """доставна × (1 + надценка/100) × (1 + ДДС/100), нагоре до стъпката."""
    price = (to_decimal(cost) * (1 + Decimal(str(markup_pct)) / 100)
             * (1 + Decimal(str(vat_pct)) / 100))
    return round_up(price, Decimal(str(step)))


if __name__ == "__main__":
    import math

    noisy = 26.25 * 1.1 * 1.2
    assert noisy != 34.65 and math.ceil(noisy / 0.05) * 0.05 > 34.69      # грешното
    assert round_up(Decimal(str(noisy)), Decimal("0.05")) == Decimal("34.70")  # и това
    assert sale_price("26,25", 10, 20) == Decimal("34.65")
    assert sale_price("26,25", 10, 20, 0.05) == Decimal("34.65")
    assert sale_price("10,00", 30, 20) == Decimal("15.60")
    assert sale_price("10,01", 30, 20) == Decimal("15.65")                  # 15,6156 → нагоре
    assert sale_price("1 234,50", 0, 0, "1") == Decimal("1235.00")
    assert round_up(Decimal("2.00"), Decimal("0.10")) == Decimal("2.00")
    assert f"{sale_price('26,25', 10, 20):.2f}".replace(".", ",") == "34,65"
    assert Decimal(0.05) != Decimal("0.05") and Decimal(str(0.05)) == Decimal("0.05")
    print("OK")
```
