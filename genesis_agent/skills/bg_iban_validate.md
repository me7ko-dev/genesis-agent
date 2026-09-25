---
name: bg_iban_validate
category: domain
description: Български IBAN (банкова сметка в България, 22 знака) — структура по Наредба
  № 13 на БНБ (BG, контролно число, BIC код на банката, БАЕ, вид сметка) и проверка по модул 97.
triggers:
- iban
- български iban
- банкова сметка iban
- валидирай iban
- проверка на iban контролно число
- bulgarian iban bank account
version: '1.0'
author: Genesis
last_updated: '2026-09-25T14:00:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-09-25: Genesis написа валидатор на
български IBAN, който проверява само дължината и модул 97 — приемаше
„BG31123496611020345678“ (цифри вместо BIC букви) и букви на мястото на
БАЕ/вида сметка, а собствените тестове бяха зелени. Второто пускане не написа код.
Източник: Наредба № 13 на БНБ за IBAN и BAE кодове, чл. 2–4 и приложения 1–3
(bnb.bg/bnbweb/groups/public/documents/bnb_law/regulations_iban_bg.pdf).

## Python Код
```python
"""Български IBAN по Наредба № 13 на БНБ.

Правила:
- 22 знака: "BG" + 2 цифри контролно число + BBAN от 18 знака.
- BBAN: 4 главни латински букви (първите 4 знака от BIC на банката)
  + 4 цифри (БАЕ) + 2 цифри (вид сметка) + 8 знака цифри ИЛИ главни букви
  (банката решава дали ползва букви там — буквите в края са валидни).
- Знаците са само 0-9 и A-Z. В електронен вид — без интервали; на хартия —
  групи по 4 с интервал („BG80 BNBG 9661 1020 3456 78“) — интервалите се махат
  преди проверката.
- Проверка: първите 4 знака отиват в края, всяка буква става число
  (A=10 … Z=35), полученото число mod 97 трябва да е 1.
- Изчисляване: със „00“ на мястото на контролното число → 98 − (число mod 97),
  с водеща нула до 2 цифри.
- Чужд IBAN (DE…, RO…) не е български, дори да е валиден.
"""
from __future__ import annotations

import re

_BG_IBAN = re.compile(r"BG\d{2}[A-Z]{4}\d{6}[A-Z0-9]{8}")


def _as_number(s: str) -> int:
    return int("".join(str(int(c, 36)) for c in s))


def iban_check_digits(bban: str) -> str:
    return f"{98 - _as_number(bban + 'BG00') % 97:02d}"


def validate_bg_iban(iban: str) -> bool:
    """Вярно само за валиден български IBAN (електронен или хартиен вид)."""
    if not isinstance(iban, str):
        return False
    s = iban.replace(" ", "")
    if not _BG_IBAN.fullmatch(s):
        return False
    return _as_number(s[4:] + s[:4]) % 97 == 1


if __name__ == "__main__":
    # Примерът от Наредба № 13, приложение 2 (сметнат там на ръка):
    # BG00AAAA12311012345678 → AAAA12311012345678BG00 → 1010101012311012345678111600
    # 1010101012311012345678111600 mod 97 = 65; 98 − 65 = 33 → BG33AAAA12311012345678
    assert iban_check_digits("AAAA12311012345678") == "33"
    assert validate_bg_iban("BG33AAAA12311012345678")
    assert validate_bg_iban("BG80BNBG96611020345678")          # пример на БНБ
    assert validate_bg_iban("BG80 BNBG 9661 1020 3456 78")     # хартиен вид
    assert not validate_bg_iban("BG34AAAA12311012345678")      # грешно контролно число
    # Букви в последните 8 знака са позволени:
    assert validate_bg_iban("BG" + iban_check_digits("UNCR70001512345ABC") + "UNCR70001512345ABC")
    # Вярно контролно число, но грешна структура → невалиден:
    for bban in ("123496611020345678", "BNBG9A611020345678", "BNBG9661X0203456AB"):
        assert not validate_bg_iban("BG" + iban_check_digits(bban) + bban)
    assert not validate_bg_iban("DE89370400440532013000")      # валиден, но германски
    assert not validate_bg_iban("BG80BNBG9661102034567")       # 21 знака
    assert not validate_bg_iban("")
    print("OK")
```
