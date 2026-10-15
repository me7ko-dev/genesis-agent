---
name: bg_excel_csv_import
category: domain
description: CSV, изтеглен от Excel с български настройки — BOM в началото (utf-8-sig), разделител ;,
  десетична запетая, неразделим интервал за хилядите, полета с ; в кавички, дати дд.мм.гггг,
  кодове с водещи нули като текст.
triggers:
- csv от excel
- изтеглен от excel csv
- експорт от excel в csv
- свален от excel
- excel csv export bom
version: '1.0'
author: Genesis
last_updated: '2026-10-02T18:00:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-10-02 (bench csv-sqlite): Genesis
отвори CSV-то на Excel с `encoding="utf-8"` 2/2 пъти — първата колона стана
`﻿ЕИК`, `row["ЕИК"]` гърмеше и всеки ред се прескачаше (1/3 и 0/3 скрити
теста). Excel „CSV UTF-8“ пише BOM; с български регионални настройки
разделителят е `;`, а десетичният знак — `,`.

## Python Код
```python
"""CSV от Excel (български настройки) → редове, числа, дати.

Факти:
- Excel „CSV UTF-8 (разделен със запетаи)“ записва BOM (EF BB BF). С encoding="utf-8"
  първото заглавие става "\\ufeffЕИК" и row["ЕИК"] дава KeyError — всеки ред изглежда
  развален. Чети с encoding="utf-8-sig": маха BOM-а, а файл без BOM чете също.
- Разделителят е ";", десетичният знак е ",": "2,40", "1 234,50".
- Хилядите са с интервал — често НЕРАЗДЕЛИМ (U+00A0), понякога тесен (U+202F):
  махни всички интервали, не само " ".
- Поле с ";" вътре е в кавички ("Иванов и син; ООД") — csv.reader/DictReader с
  delimiter=";", никога line.split(";"). Отваряй с newline="".
- Датите са дд.мм.гггг; несъществуваща (31.02) → ValueError от strptime — прескочи реда.
- Кодове с водещи нули (ЕИК, ЕГН, пощенски код) остават текст. Контролна цифра НЕ се
  проверява, освен ако заявката го иска изрично — иначе верни редове се губят.
- sqlite3 не приема Decimal като стойност — пази float (или стотинки като int).
"""
from __future__ import annotations

import csv
import datetime
import re
from collections.abc import Iterator

_SPACES = re.compile(r"[\s  ]")


def read_rows(path: str) -> Iterator[dict[str, str]]:
    """Редовете като речници по заглавието; BOM-ът е махнат."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f, delimiter=";")


def parse_number(text: str | None) -> float:
    """'1 234,50' / '12 500,00' / '980' → float; ValueError при текст или празно."""
    s = _SPACES.sub("", text or "").replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        raise ValueError(f"не е число: {text!r}")
    return float(s)


def parse_date(text: str) -> datetime.date:
    """'05.03.2019' → date(2019, 3, 5); ValueError за 31.02.2022."""
    return datetime.datetime.strptime(text.strip(), "%d.%m.%Y").date()


def format_number(value: float, places: int = 2) -> str:
    """Обратно за Excel: 1234.5 → '1234,50'."""
    return f"{value:.{places}f}".replace(".", ",")


if __name__ == "__main__":
    import os
    import tempfile

    text = ('ЕИК;Име;Оборот;От дата\n'
            '000123456;"Иванов и син; ООД";1 234,50;05.03.2019\n'
            '204512377;Зелен свят;12 500,00;17.11.2021\n')
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write(text)
    with open(path, encoding="utf-8", newline="") as f:      # грешното отваряне
        assert next(csv.reader(f, delimiter=";"))[0] == "﻿ЕИК"
    rows = list(read_rows(path))
    os.remove(path)
    assert [r["ЕИК"] for r in rows] == ["000123456", "204512377"]
    assert rows[0]["Име"] == "Иванов и син; ООД"
    assert parse_number(rows[0]["Оборот"]) == 1234.5
    assert parse_number(rows[1]["Оборот"]) == 12500.0
    assert parse_date(rows[0]["От дата"]) == datetime.date(2019, 3, 5)
    for bad in ("много", "", "1,2,3"):
        try:
            parse_number(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    try:
        parse_date("31.02.2022")
        raise AssertionError("31.02")
    except ValueError:
        pass
    assert format_number(1234.5) == "1234,50"
    print("OK")
```
