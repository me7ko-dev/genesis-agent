---
name: bg_vat_number_validate
category: domain
description: Български ДДС номер (идентификационен номер по ДДС, чл. 94 ЗДДС) — BG + 9-цифрен
  ЕИК на фирмата или BG + 10-цифрено ЕГН на физическо лице; валиден номер, контролна цифра,
  13-цифрен ЕИК на клон не е ДДС номер.
triggers:
- ддс номер
- идентификационен номер по ддс
- номер по ддс
- валидирай ддс номер
- bg + еик
- bg + егн
- bulgarian vat number
version: '1.0'
author: Genesis
last_updated: '2026-09-25T14:00:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-09-25: Genesis написа валидатор на
ДДС номер 2/2 пъти, който приема BG + 13-цифрен ЕИК (подведен от „ЕИК е 9 или 13
цифри“), а веднъж и ЕГН с несъществуваща дата (месец 13); собствените тестове
бяха зелени. Източници: чл. 94, ал. 2 ЗДДС (номерът е ЕИК, ЕГН, ЛНЧ или служебен
номер с префикс BG); форматът в системата VIES на ЕС е BG + 9 или 10 цифри;
алгоритмите — виж и уменията bg_eik_bulstat_validate и bg_egn_validate_and_decode.
Бележка: от 2022/2024 г. НАП дава на част от физическите лица BG + ЕИК от
БУЛСТАТ или BG + служебен номер вместо BG + ЕГН — ако заявката иска и ЛНЧ или
служебен номер, питай за правилата им, не ги измисляй.

## Python Код
```python
"""Български ДДС номер: BG + ЕИК (9 цифри) или BG + ЕГН (10 цифри).

Правила:
- Префиксът е точно "BG" (главни букви), следван само от цифри.
- 9 цифри → ЕИК на фирмата: 8 цифри × тегла 1..8, mod 11; остатък 10 → тегла
  3..10; пак 10 → 0. Трябва да е равно на 9-ата цифра.
- 10 цифри → ЕГН: ГГММДД + 3 цифри + контролна. Месец +20 → 1800-те, +40 →
  2000-те; датата трябва да съществува. 9 цифри × тегла 2,4,8,5,10,9,7,3,6,
  mod 11; остатък 10 → 0.
- 13-цифрен ЕИК (клон/поделение) НЕ е ДДС номер: регистрацията по ДДС е на
  юридическото лице, форматът е само BG + 9 или 10 цифри.
- Без префикс, друг префикс (RO, DE…), друга дължина → невалиден.
"""
from __future__ import annotations

import datetime


def _eik9_ok(n: str) -> bool:
    rest = sum(int(d) * w for d, w in zip(n, range(1, 9))) % 11
    if rest == 10:
        rest = sum(int(d) * w for d, w in zip(n, range(3, 11))) % 11
        if rest == 10:
            rest = 0
    return rest == int(n[8])


def _egn_ok(n: str) -> bool:
    year, month, day = int(n[:2]), int(n[2:4]), int(n[4:6])
    if month > 40:
        year, month = year + 2000, month - 40
    elif month > 20:
        year, month = year + 1800, month - 20
    else:
        year += 1900
    try:
        datetime.date(year, month, day)
    except ValueError:
        return False
    rest = sum(int(d) * w for d, w in zip(n, (2, 4, 8, 5, 10, 9, 7, 3, 6))) % 11
    return (0 if rest == 10 else rest) == int(n[9])


def validate_bg_vat(vat: str) -> bool:
    """Вярно само за BG + валиден 9-цифрен ЕИК или BG + валидно ЕГН."""
    if not isinstance(vat, str) or not vat.startswith("BG"):
        return False
    n = vat[2:]
    if not (n.isascii() and n.isdigit()):
        return False
    if len(n) == 9:
        return _eik9_ok(n)
    if len(n) == 10:
        return _egn_ok(n)
    return False


if __name__ == "__main__":
    # Сметнати на ръка:
    # ЕИК 10000000 → 1*1 = 1                                   → BG100000001
    # ЕИК 10000008 → 1 + 8*8 = 65; 65 % 11 = 10 → тегла 3..10: 3 + 80 = 83; 83 % 11 = 6 → BG100000086
    # ЕГН 750101001 → 7*2+5*4+0+1*5+0+1*9+0+0+1*6 = 54; 54 % 11 = 10 → 0    → BG7501010010
    # ЕГН 750101002 → 14+20+5+9+2*6 = 60; 60 % 11 = 5                       → BG7501010025
    # ЕГН 054101000 (2005-01-01) → 0+5*4+4*8+1*5+0+1*9 = 66; 66 % 11 = 0     → BG0541010000
    # ЕГН 751301000 (месец 13) → 66 % 11 = 0, но датата не съществува        → BG7513010000 невалиден
    assert validate_bg_vat("BG100000001")
    assert validate_bg_vat("BG100000086")
    assert not validate_bg_vat("BG100000080")          # без резервните тегла
    assert validate_bg_vat("BG7501010010")
    assert validate_bg_vat("BG7501010025")
    assert not validate_bg_vat("BG7501010026")
    assert validate_bg_vat("BG0541010000")
    assert not validate_bg_vat("BG7513010000")
    assert not validate_bg_vat("BG1000000010002")      # 13-цифрен ЕИК на клон
    assert not validate_bg_vat("100000001")            # без BG
    assert not validate_bg_vat("RO100000001")
    assert not validate_bg_vat("BG10000000a")
    assert not validate_bg_vat("")
    print("OK")
```
