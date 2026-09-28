---
name: bg_egn_validate_and_decode
category: domain
description: Българско ЕГН (единен граждански номер) — проверка на контролната цифра,
  дата на раждане с век по месеца (1800/1900/2000) и пол по деветата цифра (четна = мъж).
triggers:
- егн
- единен граждански номер
- валидирай егн
- проверка на егн контролна цифра
- дата на раждане и пол от егн
- bulgarian egn personal number
version: '1.0'
author: Genesis
last_updated: '2026-09-25T09:00:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-09-25: и четирите безплатни модела
във веригата (groq/ollama gpt-oss-120b, nvidia nemotron ultra/super) твърдят
3/3 пъти, че четна девета цифра е ЖЕНА — обратното е вярното. Написаният от тях
код и тестовете им бяха еднакво грешни, затова тестовете бяха зелени.

## Python Код
```python
"""Българско ЕГН — проверка и разчитане.

Правила:
- 10 цифри: ГГММДД, три цифри (район + пореден номер), контролна цифра.
- Векът се носи от месеца: 01–12 → 1900–1999; 21–32 → 1800–1899 (месец − 20);
  41–52 → 2000–2099 (месец − 40). Датата трябва да съществува (29.02 само във
  високосна година; 1900 НЕ е високосна, 2000 е).
- Пол по деветата цифра: ЧЕТНА → мъж ("М"), НЕЧЕТНА → жена ("Ж").
- Контролна цифра: тегла 2, 4, 8, 5, 10, 9, 7, 3, 6 върху първите девет цифри,
  сумата mod 11; остатък 10 → 0.
"""
from __future__ import annotations

import datetime

EGN_WEIGHTS = (2, 4, 8, 5, 10, 9, 7, 3, 6)


def egn_checksum(first9: str) -> int:
    """Контролната цифра за първите девет цифри."""
    rest = sum(int(d) * w for d, w in zip(first9, EGN_WEIGHTS)) % 11
    return 0 if rest == 10 else rest


def egn_birth_date(egn: str) -> datetime.date:
    """Датата на раждане. ValueError, ако месецът/датата са невъзможни."""
    yy, mm, dd = int(egn[0:2]), int(egn[2:4]), int(egn[4:6])
    if 1 <= mm <= 12:
        year = 1900 + yy
    elif 21 <= mm <= 32:
        year, mm = 1800 + yy, mm - 20
    elif 41 <= mm <= 52:
        year, mm = 2000 + yy, mm - 40
    else:
        raise ValueError(f"невалиден месец в ЕГН: {egn[2:4]}")
    return datetime.date(year, mm, dd)


def validate_egn(egn: str) -> bool:
    """Вярно само за валидно ЕГН: 10 цифри, съществуваща дата, вярна контролна цифра."""
    if not isinstance(egn, str) or len(egn) != 10 or not egn.isdigit():
        return False
    try:
        egn_birth_date(egn)
    except ValueError:
        return False
    return egn_checksum(egn[:9]) == int(egn[9])


def parse_egn(egn: str) -> dict[str, str]:
    """{"birth_date": "ГГГГ-ММ-ДД", "gender": "М"/"Ж"}; ValueError при невалидно ЕГН."""
    if not validate_egn(egn):
        raise ValueError(f"невалидно ЕГН: {egn!r}")
    gender = "М" if int(egn[8]) % 2 == 0 else "Ж"
    return {"birth_date": egn_birth_date(egn).isoformat(), "gender": gender}


if __name__ == "__main__":
    # Изчислено на ръка: 7*2+5*4+2*8+3*5+1*10+6*9+9*7+2*3+6*6 = 234; 234 % 11 = 3.
    assert validate_egn("7523169263")
    assert parse_egn("7523169263") == {"birth_date": "1875-03-16", "gender": "М"}
    assert not validate_egn("7523169264")          # грешна контролна цифра
    assert not validate_egn("752316926")           # 9 цифри
    assert not validate_egn("75231692a3")          # буква
    first9 = "000229111"                           # 29.02.1900 — 1900 не е високосна
    assert not validate_egn(first9 + str(egn_checksum(first9)))
    first9 = "004229111"                           # 29.02.2000 — високосна
    assert parse_egn(first9 + str(egn_checksum(first9))) == {"birth_date": "2000-02-29", "gender": "Ж"}
    print("OK")
```
