---
name: bg_invoice_fields
category: domain
description: Полета от текста на българска фактура (след pdfplumber) — номер с водещи
  нули, дата „ДД.ММ.ГГГГ г.“, суми „12 440,00 лв.“, доставчик в кавички, ЕИК както е
  отпечатан; етикетът и стойността са на един ред.
triggers:
- фактура
- фактури
- фактура №
- данъчна основа
- сума за плащане
- доставчик
- pdfplumber фактура
- bulgarian invoice pdf fields
version: '1.0'
author: Genesis
last_updated: '2026-09-30T00:00:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-09-26…29 (bench `faktura-excel`,
7 пуска, 0 верни): регексите на Genesis минаваха собствените му примерни PDF-и,
но не и фактура във вида, в който идва от фирма. Грешките: `Фактура\s*№?\s*(\S+)`
с IGNORECASE хваща заглавието „ФАКТУРА“ и прескача на следващия ред („Оригинал“);
датата се търсеше само като ГГГГ-ММ-ДД, а на фактурата е „03.09.2026 г.“;
ЕИК се отхвърляше по контролната цифра (знанието за ЕИК е за проверка, не за
извличане) и цялата фактура се губеше. pdfplumber слива две колони в един ред с
един интервал: „Доставчик: ЕТ „Иван“ Получател: „Клиент“ АД“ (проверено с
reportlab + pdfplumber). Номерът — 10 цифри (чл. 78 ППЗДДС).

## Python Код
```python
"""Полета от текста на българска фактура (pdfplumber `page.extract_text()`).

Правила:
- Етикетът и стойността са на ЕДИН ред: между тях само хоризонтален интервал и
  „:“. Никога `\\s*` — той прескача нов ред: заглавието „ФАКТУРА“ на свой ред +
  re.IGNORECASE → `Фактура\\s*№?\\s*(\\S+)` връща „Оригинал“ от следващия ред.
  „№“ е задължителна част от етикета на номера.
- Етикетът се търси първо в НАЧАЛОТО на реда, после където и да е (две колони
  на един ред): „Сума с ДДС 20%: 120,00“ стои преди „ДДС 20%: 20,00“, а само
  второто е ДДС-то.
- Номер: 10 цифри с водещи нули (чл. 78 ППЗДДС) — НИЗ, не int (0000000981).
- Дата: „ДД.ММ.ГГГГ г.“ / „Д.М.ГГГГг.“ / ДД/ММ/ГГГГ / ДД-ММ-ГГГГ / ГГГГ-ММ-ДД →
  ISO през datetime.date (несъществуваща дата → грешка, не тих низ).
- Суми: „12 440,00 лв.“, „1 234,56 €“, „1.234,56“, „1234.56“; хилядите — интервал,
  NBSP (U+00A0), тесен NBSP (U+202F) или точка; десетичната — запетая (точка само
  без друг разделител). Валутата (лв., лева, BGN, €, EUR) не е част от числото.
- Доставчик: остатъкът от реда С КАВИЧКИТЕ „…“ и правната форма (ЕООД, ООД, АД,
  ЕТ); отрязва се при следващ етикет от втората колона (Получател, ЕИК, …).
- ЕИК: 9 или 13 цифри, както е отпечатан. При ИЗВЛИЧАНЕ не се отхвърля по
  контролна цифра — това е предупреждение, не изключение (иначе една грешна
  цифра спира цялата папка). Проверката е в bg_eik_bulstat_validate.
- Основа + ДДС ≠ за плащане (разлика > 0.01) → предупреждение: вероятно е
  прочетено грешно поле.
- Собствените тестови PDF-и: заглавие „ФАКТУРА“ на отделен ред, „Оригинал“,
  дата с „ г.“, суми с интервал за хилядите и „лв.“/€, номер с водещи нули,
  доставчик в кавички, две колони на един ред — не само вида, който регексите
  очакват.
"""
from __future__ import annotations

import datetime
import re

_H = r"[ \t  ]"                 # хоризонтален интервал — НЕ \s
_CURRENCY = r"(?:лв\.?|лева|BGN|EUR|€)"
NUMBER = r"\d{1,10}"
DATE = r"\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{2}-\d{2}"
AMOUNT = r"\d{1,3}(?:[   .]\d{3})+(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?"
EIK = r"\d{13}|\d{9}"
_NEXT_LABEL = re.compile(r"\s+(?:Получател|ЕИК|Идент|ИН по ЗДДС|ДДС №|Адрес|МОЛ|Тел)\b.*$")


def find_after(text: str, label: str, value: str) -> str | None:
    """Стойността след етикета на СЪЩИЯ ред; първо етикет в началото на реда."""
    lab = f"{_H}*".join(re.escape(w) for w in label.split())
    tail = rf"{_H}*[:\-–]?{_H}*(?:{_CURRENCY}{_H}*)?({value})(?![\d,.])"
    for rx in (rf"^{_H}*{lab}{tail}", rf"(?<!\w){lab}{tail}"):
        m = re.search(rx, text, re.MULTILINE | re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def parse_amount(s: str) -> float:
    s = re.sub(r"[   ]", "", s)
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", s):
        s = s.replace(".", "")
    return round(float(s), 2)


def parse_date(s: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        y, m, d = s.split("-")
    else:
        d, m, y = re.split(r"[./-]", s)
    return datetime.date(int(y), int(m), int(d)).isoformat()


def supplier(text: str, label: str = "Доставчик") -> str | None:
    raw = find_after(text, label, r"[^\n]+")
    return _NEXT_LABEL.sub("", raw).strip(" ,;") if raw else None


def invoice_fields(text: str) -> tuple[dict, list[str]]:
    """Полетата + предупрежденията (никога изключение заради ЕИК или сумите)."""
    got = {
        "number": find_after(text, "Фактура №", NUMBER),
        "date": find_after(text, "Дата", DATE),
        "supplier": supplier(text),
        "eik": find_after(text, "ЕИК", EIK),
        "net": find_after(text, "Данъчна основа", AMOUNT),
        "vat": find_after(text, "ДДС 20%", AMOUNT),
        "total": find_after(text, "Сума за плащане", AMOUNT),
    }
    warnings = [f"няма {k}" for k, v in got.items() if v is None]
    if got["date"]:
        got["date"] = parse_date(got["date"])
    for k in ("net", "vat", "total"):
        if got[k]:
            got[k] = parse_amount(got[k])
    if got["eik"] and not _eik_ok(got["eik"]):
        warnings.append(f"ЕИК {got['eik']}: контролната цифра не съвпада")
    if all(isinstance(got[k], float) for k in ("net", "vat", "total")) \
            and abs(got["net"] + got["vat"] - got["total"]) > 0.01:
        warnings.append("основа + ДДС ≠ сума за плащане")
    return got, warnings


def _eik_ok(eik: str) -> bool:  # 9-а цифра; пълната проверка: bg_eik_bulstat_validate
    d = [int(c) for c in eik[:9]]
    r = sum(x * w for x, w in zip(d, range(1, 9))) % 11
    if r == 10:
        r = sum(x * w for x, w in zip(d, range(3, 11))) % 11 % 10
    return r == d[8]


if __name__ == "__main__":
    one = ("ФАКТУРА\nОригинал\nФактура № 0000000981\nДата: 15.01.2026 г.\n"
           "Доставчик: „Зелена долина“ ООД\nЕИК: 100000097\nПолучател: Мария Иванова\n"
           "Данъчна основа: 1 234,56 €\nДДС 20%: 246,91 €\nСума за плащане: 1 481,47 €\n")
    got, warn = invoice_fields(one)
    assert got == {"number": "0000000981", "date": "2026-01-15",
                   "supplier": "„Зелена долина“ ООД", "eik": "100000097",
                   "net": 1234.56, "vat": 246.91, "total": 1481.47}, got
    assert warn == [], warn
    # Две колони на един ред, „Сума с ДДС 20%“ преди „ДДС 20%“, грешна контролна цифра.
    two = ("ФАКТУРА № 0000012345 Дата: 5.2.2026г.\n"
           "Доставчик: ЕТ „Иван Иванов – 55“ Получател: „Клиент“ АД\n"
           "ЕИК: 123456789 ЕИК: 100000001\n"
           "Сума с ДДС 20%: 120,00 лв.\nДанъчна основа: 100,00 лв.\n"
           "ДДС 20%: 20,00 лв.\nСума за плащане: 120,00 лв.\n")
    got, warn = invoice_fields(two)
    assert got == {"number": "0000012345", "date": "2026-02-05",
                   "supplier": "ЕТ „Иван Иванов – 55“", "eik": "123456789",
                   "net": 100.0, "vat": 20.0, "total": 120.0}, got
    assert warn == ["ЕИК 123456789: контролната цифра не съвпада"], warn
    # Само заглавие, без „№“ — номерът НЕ е следващият ред.
    got, warn = invoice_fields("ФАКТУРА\nОригинал\nДата: 2026-03-31\n")
    assert got["number"] is None and got["date"] == "2026-03-31"
    assert parse_amount("1.234,56") == 1234.56 and parse_amount("1234.56") == 1234.56
    assert parse_amount("10 000,5") == 10000.5 and parse_amount("1.234") == 1234.0
    assert _eik_ok("100000086") and not _eik_ok("100000080")
    try:
        parse_date("30.02.2026")
        raise AssertionError("30.02 не съществува")
    except ValueError:
        pass
    print("OK")
```
