---
name: bg_shop_scrape_prices
category: domain
min_score: 3
description: Цени и страници от български онлайн магазин — „1 299,00 лв.“ с неразделим интервал
  (U+00A0), старата цена в <s>, windows-1251 само в <meta charset>, относителни връзки „Следваща“
  и каталог, който сочи пак към началото.
triggers:
- scraper онлайн магазин каталог
- windows-1251 meta charset
- scrape продуктите от магазин
version: '1.0'
author: Genesis
last_updated: '2026-10-04T02:40:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-10-01 и 2026-10-02 (bench
shop-scraper): цената „1 299,00 лв.“ идва с НЕРАЗДЕЛИМ интервал (`&nbsp;`,
U+00A0) — `replace(" ", "")` не го маха и `float()` гърми или продуктът
се губи. `requests` при `Content-Type: text/html` без charset приема
ISO-8859-1, а страницата е windows-1251 (казано само в `<meta charset>`).

## Python Код
```python
"""Цени и страници от български онлайн магазин.

Факти:
- „1 299,00 лв.“: хилядите са с интервал, често НЕРАЗДЕЛИМ (U+00A0, &nbsp;) или тесен
  (U+202F) — махни ВСИЧКИ интервали (re.sub(r"\\s", "", …) ги хваща), после "," → ".".
  Махни и „лв.“ / „лв“ / „€“ / „EUR“.
- Промоция: <s>499,00 лв.</s> 449,00 лв. — махни <s>, <del> и <strike> ПРЕДИ да вземеш
  текста (BeautifulSoup: for old in el.select("s, del, strike"): old.decompose()).
- Кодировка: requests дава r.encoding = "ISO-8859-1", когато заглавката е само text/html.
  Не ползвай r.text — декодирай r.content по <meta charset> (виж decode_html) или подай
  байтовете на BeautifulSoup(r.content, "html.parser"), който чете meta сам.
- Връзките са относителни („?page=2“, „/catalog“) → urljoin(текущия_url, href).
- Обхождане: множество от посетените АБСОЛЮТНИ адреси — последната страница може да сочи
  пак към първата. Продукт, повторен на друга страница („Препоръчано“), се пази веднъж —
  по реда на първата поява.
"""
from __future__ import annotations

import re

_NOT_NUMBER = re.compile(r"\s|лв\.?|€|eur", re.IGNORECASE)
_META = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?([\w-]+)""", re.IGNORECASE)


def parse_bg_price(text: str) -> float:
    """'1\\xa0299,00 лв.' / '8,50 лв.' / '39.99 €' → float; ValueError, ако не е цена."""
    s = _NOT_NUMBER.sub("", text or "").replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", s):
        raise ValueError(f"не е цена: {text!r}")
    return float(s)


def decode_html(raw: bytes, header_charset: str | None = None) -> str:
    """Байтовете на страницата → текст: charset от заглавката, иначе от <meta>, иначе utf-8."""
    match = _META.search(raw[:4096])
    charset = header_charset or (match.group(1).decode("ascii") if match else "utf-8")
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


if __name__ == "__main__":
    from urllib.parse import urljoin

    price = "1\xa0299,00 лв."
    try:                                           # грешното: само обикновеният интервал
        float(price.replace(" ", "").replace("лв.", "").replace(",", "."))
        raise AssertionError("nbsp")
    except ValueError:
        pass
    assert parse_bg_price(price) == 1299.0
    assert parse_bg_price("1 299,00 лв.") == 1299.0
    assert parse_bg_price("8,50 лв.") == 8.5 and parse_bg_price("32,90 лв") == 32.9
    assert parse_bg_price("39.99 €") == 39.99 and parse_bg_price("12 500 лв.") == 12500.0
    for bad in ("", "Изчерпан", "по договаряне", "1,2,3 лв."):
        try:
            parse_bg_price(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    page = '<html><head><meta charset="windows-1251"></head><body>Лаптоп 1\xa0299,00 лв.</body>'
    raw = page.encode("cp1251")
    assert raw.decode("latin-1") != page                 # какво дава r.text без charset
    assert decode_html(raw) == page
    assert decode_html("Чаша".encode()) == "Чаша"
    assert decode_html(b"<meta charset='nope'>x") == "<meta charset='nope'>x"
    assert urljoin("http://shop.bg/catalog?page=2", "?page=3") == "http://shop.bg/catalog?page=3"
    assert urljoin("http://shop.bg/catalog?page=3", "/catalog") == "http://shop.bg/catalog"
    print("OK")
```
