---
name: bg_phone_numbers
category: domain
description: Български телефонни номера — мобилен или стационарен по номерационния план,
  нормализиране до +359…, всички обичайни изписвания (0888/123-456, +359 888 123 456, 00359…).
triggers:
- български мобилен
- мобилен номер
- мобилен телефон
- телефонен номер
- телефонни номера
- телефоните
- bulgarian phone number
- bulgarian mobile number
version: '1.0'
author: Genesis
last_updated: '2026-10-12T13:00:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-10-12 (bench booking-form, Claude
през абонамента): валидаторът приемаше само изброените в заявката изписвания и
отхвърляше „0898/123-456“ — в България мобилен номер често се пише с „/“ и „-“
(скритият тест падна 19/20). Източник за кои номера са мобилни и кои стационарни:
номерационният план на България в libphonenumber на Google (PhoneNumberMetadata.xml,
territory BG, mobile/fixedLine, изтеглено 2026-10-12): мобилни (87|88|89|98) + 7
цифри, (43[07-9]) + 5 и 99[69] + 6; национален префикс 0, международен 00, код 359.

## Python Код
```python
"""Български телефонни номера (по номерационния план, libphonenumber BG).

Правила:
- Изписване: интервали, „-“, „/“, „.“ и скоби са само форматиране — махат се.
  „+359…“, „00359…“ и „0…“ (национален префикс) са едно и също; „359…“ без
  „+“/„00“ не е международен запис.
- Мобилен (национално значим номер, без водещата 0): 87, 88, 89 или 98 + 7
  цифри (9 общо); също 43[0,7,8,9] + 5 цифри и 99[6,9] + 6 цифри.
- Стационарен: София 2 + 5–7 цифри; други градове — кодът на града (напр. 32
  Пловдив, 52 Варна) + 5–6 цифри (общо 7–8 цифри след 0).
- Нормализиран вид: "+359" + национално значимият номер (без 0).
- „02 987 6543“ е стационарен, не мобилен; „0888 12“ е непълен — невалиден.
"""
from __future__ import annotations

import re

_FORMATTING = re.compile(r"[\s\-/.()]")
_MOBILE = re.compile(r"(?:43[07-9]|99[69]\d)\d{5}|(?:8[7-9]|98)\d{7}")
_FIXED = re.compile(r"2\d{5,7}|(?:43[1-6]|70[1-9])\d{4,5}"
                    r"|(?:[36]\d|4[124-7]|[57][1-9]|8[1-6]|9[1-7])\d{5,6}")


def national_number(phone: str) -> str | None:
    """Национално значимият номер (без 0 и без 359) или None при непознат запис."""
    if not isinstance(phone, str):
        return None
    s = _FORMATTING.sub("", phone.strip())
    if s.startswith("+359"):
        s = s[4:]
    elif s.startswith("00359"):
        s = s[5:]
    elif s.startswith("0") and not s.startswith("00"):
        s = s[1:]
    else:
        return None
    return s if s.isdigit() and s.isascii() else None


def is_mobile(phone: str) -> bool:
    n = national_number(phone)
    return bool(n and _MOBILE.fullmatch(n))


def is_fixed_line(phone: str) -> bool:
    n = national_number(phone)
    return bool(n and _FIXED.fullmatch(n))


def normalize(phone: str) -> str | None:
    """+359XXXXXXXXX за валиден мобилен или стационарен номер, иначе None."""
    n = national_number(phone)
    if n and (_MOBILE.fullmatch(n) or _FIXED.fullmatch(n)):
        return "+359" + n
    return None


if __name__ == "__main__":
    for p in ("0888 123 456", "0888123456", "+359 888 123 456", "0898/123-456",
              "00359888123456", "(0887) 12-34-56", "0988.123.456"):
        assert is_mobile(p), p
    assert normalize("0888/123-456") == "+359888123456"
    assert normalize("00359898765432") == "+359898765432"
    assert normalize("+359 888 123456") == "+359888123456"
    assert normalize("02 987 6543") == "+35929876543" and not is_mobile("02 987 6543")
    assert is_fixed_line("032 123 456") and not is_mobile("032 123 456")    # Пловдив
    for bad in ("0888 12", "", "359888123456", "888123456", "0888 123 4567", "08881234a6",
                "+44 20 7946 0958", "0888١23456"):
        assert normalize(bad) is None and not is_mobile(bad), bad
    print("OK")
```
