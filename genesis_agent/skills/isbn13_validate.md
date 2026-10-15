---
name: isbn13_validate
category: domain
description: ISBN-13 — махни тирета и интервали, точно 13 цифри, префикс 978/979, контролна
  цифра с тегла 1,3,1,3… (10 − сума mod 10) mod 10; 978-954-529-301-5 е невалиден (трябва 6).
triggers:
- isbn
- isbn контролна цифра
- isbn-13
- провери isbn
- валиден isbn
- валидирай isbn
version: '1.0'
author: Genesis
last_updated: '2026-10-11T21:30:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-10-11 (bench library-pkg, пуск 2):
`add_book("978-954-529-301-5")` мина, а контролната цифра е 6 — проверката
беше написана грешно и собствените тестове на модела я одобриха.
Източник: ISO 2108 / International ISBN Agency, „ISBN Users' Manual“, §5
(check digit for ISBN-13: тегла 1 и 3, модул 10).

## Python Код
```python
"""ISBN-13: нормализиране и контролна цифра.

Факти (ISO 2108, ISBN Users' Manual):
- Тиретата и интервалите са само за четене — махни ги; остават ТОЧНО 13 цифри.
- Префиксът е 978 или 979 (ISBN-13 е EAN-13 с тези префикси).
- Контролна цифра: първите 12 цифри с тегла 1,3,1,3,…; c = (10 − сума mod 10) mod 10.
  Сума, кратна на 10 → c = 0 (не 10).
- ISBN-10 (9 цифри + цифра/X) е друг формат с модул 11 — не го приемай за ISBN-13.
- Пази и сравнявай нормализирания низ от 13 цифри: "978-954-…" и "978954…" са една книга.
"""
from __future__ import annotations


def normalize_isbn13(raw: str) -> str:
    """'978-954-529-301-6' → '9789545293016'; невалиден → ValueError."""
    digits = "".join(str(raw).replace("-", " ").split())
    if len(digits) != 13 or not digits.isdigit():
        raise ValueError(f"ISBN-13 трябва да е 13 цифри: {raw!r}")
    if digits[:3] not in ("978", "979"):
        raise ValueError(f"ISBN-13 започва с 978 или 979: {raw!r}")
    if check_digit(digits[:12]) != int(digits[12]):
        raise ValueError(f"грешна контролна цифра: {raw!r}")
    return digits


def check_digit(first12: str) -> int:
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(first12))
    return (10 - total % 10) % 10


def is_valid_isbn13(raw: str) -> bool:
    try:
        normalize_isbn13(raw)
    except ValueError:
        return False
    return True


if __name__ == "__main__":
    assert check_digit("978954529301") == 6
    assert normalize_isbn13("978-954-529-301-6") == "9789545293016"
    assert normalize_isbn13(" 978 0 306 40615 7 ") == "9780306406157"   # примерът от ръководството
    assert not is_valid_isbn13("978-954-529-301-5")                     # грешна контролна цифра
    assert not is_valid_isbn13("97895452930")                           # 11 цифри
    assert not is_valid_isbn13("abc") and not is_valid_isbn13("")
    assert not is_valid_isbn13("0306406152")                            # ISBN-10, не 13
    assert not is_valid_isbn13("1234567890128")                         # без 978/979
    assert check_digit("979100000000") == 8 and is_valid_isbn13("979-10-00000-00-8")
    assert is_valid_isbn13("9780000000002")                             # 9+21+8 = 38 → 2
    print("OK")
```
