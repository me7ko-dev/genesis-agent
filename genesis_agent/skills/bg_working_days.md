---
name: bg_working_days
category: domain
description: Работни дни и официални празници в България по чл. 154 КТ — православен
  Великден, прехвърляне на празник от събота/неделя, 1 ноември е работен ден.
triggers:
- работни дни
- работен ден
- официални празници
- почивни дни
- неприсъствени дни
- кодекса на труда празници
- bulgarian working days public holidays
version: '1.0'
author: Genesis
last_updated: '2026-09-25T17:00:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-09-25: без него Genesis
прехвърляше празник от уикенда на следващия ден дори когато той също е
празник (Коледа 2022 → 26.12 вместо 27–28.12; 1 май 2021 → 3.05, който е
Великденски понеделник, вместо 4.05). Източници: чл. 154 от Кодекса на труда
(ал. 2 — ДВ бр. 105/2016, в сила от 01.01.2017), ГИТ 22.04.2024 („6 май не се
компенсира“ — 7 май 2024 е работен).

## Python Код
```python
"""Работни дни в България по чл. 154 от Кодекса на труда.

Правила:
- Празници (ал. 1): 1.01, 3.03, 1.05, 6.05, 24.05, 6.09, 22.09, 24.12, 25.12,
  26.12 и Велики петък, Велика събота, Великден (неделя) и понеделник —
  по ПРАВОСЛАВНИЯ Великден (юлиански изчислен, +13 дни за 1900–2099).
- 1 ноември (Ден на народните будители) е неприсъствен САМО за учебните
  заведения — за всички останали е РАБОТЕН ден и не се прехвърля.
- Ал. 2 (от 2017): празник в събота/неделя → първият или първите два РАБОТНИ
  дни след него са почивни. „Работен“ = не уикенд, не празник, не вече
  прехвърлен: 24–25.12 в събота/неделя и 26.12 в понеделник → 27 и 28.12;
  1.05 в събота, а 3.05 е Великденски понеделник → 4.05.
- Великденските дни НЕ се компенсират (и когато съвпаднат с друг празник:
  6.05.2024 = Великденски понеделник → 7.05.2024 е работен).
- Еднократните решения на Министерския съвет (ал. 3) не са правило — не се
  предвиждат.
"""
from __future__ import annotations

import datetime

FIXED_HOLIDAYS = [(1, 1), (3, 3), (5, 1), (5, 6), (5, 24), (9, 6), (9, 22),
                  (12, 24), (12, 25), (12, 26)]
_DAY = datetime.timedelta(days=1)


def orthodox_easter(year: int) -> datetime.date:
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month, day = divmod(d + e + 114, 31)
    return datetime.date(year, month, day + 1) + datetime.timedelta(days=13)


def days_off(year: int) -> set[datetime.date]:
    """Празниците на годината + прехвърлените от уикенда (без съботите/неделите)."""
    easter = orthodox_easter(year)
    off = {easter + datetime.timedelta(days=k) for k in (-2, -1, 0, 1)}
    fixed = sorted(datetime.date(year, m, d) for m, d in FIXED_HOLIDAYS)
    off.update(fixed)
    for holiday in fixed:  # по ред: всеки уикенд празник взима следващия свободен работен ден
        if holiday.weekday() >= 5:
            day = holiday + _DAY
            while day.weekday() >= 5 or day in off:
                day += _DAY
            off.add(day)
    return off


def is_working_day(day: datetime.date) -> bool:
    # Прехвърленото от 24–26.12 стига най-много до 28.12 — годината е същата.
    return day.weekday() < 5 and day not in days_off(day.year)


def count_working_days(start: datetime.date, end: datetime.date) -> int:
    """Работните дни от start до end включително."""
    n, day = 0, start
    while day <= end:
        n += is_working_day(day)
        day += _DAY
    return n


if __name__ == "__main__":
    D = datetime.date
    assert orthodox_easter(2024) == D(2024, 5, 5)
    assert orthodox_easter(2026) == D(2026, 4, 12)
    assert orthodox_easter(2027) == D(2027, 5, 2)
    assert not is_working_day(D(2026, 4, 10)) and not is_working_day(D(2026, 4, 13))
    assert is_working_day(D(2027, 11, 1))                      # 1 ноември — работен
    assert is_working_day(D(2026, 11, 2))                      # 1.11 неделя — без компенсация
    assert not is_working_day(D(2026, 5, 25))                  # 24.05 неделя
    assert not is_working_day(D(2021, 5, 4)) and is_working_day(D(2021, 5, 5))
    assert not is_working_day(D(2022, 12, 27)) and not is_working_day(D(2022, 12, 28))
    assert is_working_day(D(2022, 12, 29))
    assert is_working_day(D(2024, 5, 7))                       # ГИТ 22.04.2024
    assert not is_working_day(D(2023, 1, 2))                   # 1.01 неделя
    # 2026: 261 делнични − 12 почивни (1.01, 3.03, 10.04, 13.04, 1.05, 6.05,
    # 25.05, 7.09, 22.09, 24.12, 25.12, 28.12) = 249
    assert count_working_days(D(2026, 1, 1), D(2026, 12, 31)) == 249
    print("OK")
```
