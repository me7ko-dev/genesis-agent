---
name: bg_eik_bulstat_validate
category: domain
description: Български ЕИК / БУЛСТАТ на фирма (9 или 13 цифри) — проверка на контролните
  цифри, включително резервните тегла при остатък 10.
triggers:
- еик
- булстат
- единен идентификационен код
- валидирай еик
- проверка на булстат контролна цифра
- bulgarian eik bulstat company number
version: '1.0'
author: Genesis
last_updated: '2026-09-25T12:00:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-09-25: Genesis написа ЕИК валидатор
2/2 пъти без резервните тегла (3–10 за 9-цифрен, 4,9,5,7 за 13-цифрен) — всички
ЕИК с първи остатък 10 се отхвърляха като невалидни, а собствените тестове бяха
зелени. Източник: алгоритъмът на Агенцията по вписванията (напр.
tsvetanv.wordpress.com/2011/04/01/eik/).

## Python Код
```python
"""Български ЕИК / БУЛСТАТ — проверка на контролните цифри.

Правила:
- 9 цифри (фирма) или 13 цифри (клон/поделение): първите 9 са ЕИК на фирмата.
- 9-а цифра: първите 8 цифри × тегла 1..8, сума mod 11. Остатък 10 → същото
  с тегла 3..10; ако пак е 10 → цифрата е 0.
- 13-а цифра: цифри 9..12 (1-базово) × тегла 2, 7, 3, 5, mod 11. Остатък 10 →
  тегла 4, 9, 5, 7; ако пак е 10 → 0.
"""
from __future__ import annotations


def _check_digit(digits: str, weights: tuple[int, ...], fallback: tuple[int, ...]) -> int:
    rest = sum(int(d) * w for d, w in zip(digits, weights)) % 11
    if rest == 10:
        rest = sum(int(d) * w for d, w in zip(digits, fallback)) % 11
        if rest == 10:
            rest = 0
    return rest


def eik_check_digit_9(first8: str) -> int:
    return _check_digit(first8, (1, 2, 3, 4, 5, 6, 7, 8), (3, 4, 5, 6, 7, 8, 9, 10))


def eik_check_digit_13(digits_9_to_12: str) -> int:
    return _check_digit(digits_9_to_12, (2, 7, 3, 5), (4, 9, 5, 7))


def validate_eik(eik: str) -> bool:
    """Вярно само за валиден 9- или 13-цифрен ЕИК с верни контролни цифри."""
    if not isinstance(eik, str) or len(eik) not in (9, 13) or not eik.isdigit():
        return False
    if eik_check_digit_9(eik[:8]) != int(eik[8]):
        return False
    return len(eik) == 9 or eik_check_digit_13(eik[8:12]) == int(eik[12])


if __name__ == "__main__":
    # Сметнати на ръка:
    # 10000000 → 1*1 = 1                         → 100000001
    # 10000009 → 1*1 + 9*8 = 73; 73 % 11 = 7     → 100000097
    # 10000008 → 1*1 + 8*8 = 65; 65 % 11 = 10 → тегла 3..10: 1*3 + 8*10 = 83; 83 % 11 = 6 → 100000086
    # 13 цифри: 100000001 + 000, цифри 9..12 = "1000" → 1*2 = 2 → 1000000010002
    assert validate_eik("100000001")
    assert validate_eik("100000097")
    assert eik_check_digit_9("10000008") == 6
    assert validate_eik("100000086")
    assert not validate_eik("100000080")      # без резервните тегла би излязло 10 → грешно
    assert not validate_eik("100000002")
    assert validate_eik("1000000010002")
    assert not validate_eik("1000000010003")
    assert not validate_eik("10000000")
    assert not validate_eik("10000000a")
    print("OK")
```
