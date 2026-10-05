---
name: bg_contact_form_phone
category: domain
min_score: 3
description: Български телефон → +359… (00359 преди 0) — мобилен само с 87/88/89/98/99 и 9
  цифри, когато заявката казва „мобилен“, иначе и стационарен (02 981 23 45 → +35929812345);
  празно → грешка само при required=True; Flask 3 — escape от markupsafe, без
  before_first_request, POST → 303 точно към поискания път.
triggers:
- български мобилен номер
- телефон +359 00359
- мобилен телефон номер
- форма за запитване flask
version: '1.0'
author: Genesis
last_updated: '2026-10-05T18:00:00+00:00'
---

## Описание
Проверено знание, не по памет. Измерено 2026-10-01 (clients-migrate) и 2026-10-02
(bench contact-form, 1/2): Genesis маха първата `0` и слага `+359` отпред —
`00359888123456` става `+3590359888123456`. В 2 от 4 пуска на contact-form
приложението не тръгна изобщо: `from flask import escape` (Flask 3.0 го махна —
`markupsafe.escape`). Източник за кодовете: КРС, Национален номерационен план
(https://crc.bg/files/_en/bulgarian_NNP-en-2014(ver..2016).pdf) — мобилните са
9 цифри след 0 и започват с 87, 88, 89, 98 или 99.

## Python Код
```python
"""Български телефон → +359…; мобилен или всеки; Flask 3 форма без капаните.

Факти:
- Записите на един номер: 0888 123 456 · 0888-123-456 · +359 888 123 456 · 00359888123456
  · (0888) 123 456. Махни интервалите (и неразделимия U+00A0), -, ., /, ( и ).
- Редът на префиксите: "+359", после "00359", ЧАК ТОГАВА "0". Ако първо махнеш водещата
  "0", 00359888123456 става +3590359888123456 — грешката, измерена два пъти.
- Мобилен ли, или просто телефон? Ако заявката казва „мобилен“ → normalize_bg_mobile:
  след префикса ТОЧНО 9 цифри с код 87/88/89/98/99 (КРС); стационарен (+359 2 …) → невалиден.
  Ако иска само „международен вид / +359“ → normalize_bg_phone: и стационарните остават
  (02 981 23 45 → +35929812345, 032 123 456 → +35932123456). Измерено 2026-10-05
  (bench clients-migrate 0/2): mobile-проверка там записа стационарния номер като NULL.
- Празно поле: required=True (задължително — „всички полета са задължителни“) → None,
  тоест грешка 400; required=False → "". Без подразбиране: измерено 2026-10-05 (bench
  booking-form, 2 от 3 провала) — "" минаваше за „валиден“ при задължителен телефон.
- Flask 3.0 махна flask.escape и flask.Markup: `from markupsafe import escape`; махнат е и
  @app.before_first_request (2026-10-05: booking-form 0/25 — приложението не тръгна) —
  таблиците се създават направо в create_app(), с `with app.app_context()` при нужда.
  render_template_string и шаблоните .html ескейпват {{ value }} сами — не ползвай |safe
  за въведеното от потребителя.
- Post/Redirect/Get: `return redirect(url_for("thanks"), code=303)` — Location е точно
  поисканият път (/thanks), без ?id=… или други параметри, освен ако заявката ги иска.
- Невалидно → `render_template_string(FORM, values=request.form, error=msg), 400` —
  същата форма, със съобщението и въведеното (value="{{ values.get('name', '') }}").
"""
from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[\s  ().\-/]")
_MOBILE = re.compile(r"(?:8[789]|9[89])\d{7}")


def normalize_bg_mobile(raw: str | None, *, required: bool) -> str | None:
    """'0888 123 456' / '+359 888 123 456' / '00359888123456' → '+359888123456';
    празно → None при required, иначе ''; невалиден → None."""
    s = _SEPARATORS.sub("", raw or "")
    if not s:
        return None if required else ""
    for prefix in ("+359", "00359", "0"):
        if s.startswith(prefix):
            rest = s[len(prefix):]
            return "+359" + rest if _MOBILE.fullmatch(rest) else None
    return None


_NATIONAL = re.compile(r"[2-9]\d{7,8}")  # след 0: 8 цифри стационарен, 9 мобилен


def normalize_bg_phone(raw: str | None, *, required: bool) -> str | None:
    """Всеки български номер (и стационарен): '02 981 23 45' → '+35929812345';
    празно → None при required, иначе ''; невалиден → None. Префиксите: +359, 00359, 0."""
    s = _SEPARATORS.sub("", raw or "")
    if not s:
        return None if required else ""
    for prefix in ("+359", "00359", "0"):
        if s.startswith(prefix):
            rest = s[len(prefix):]
            return "+359" + rest if _NATIONAL.fullmatch(rest) else None
    return None


if __name__ == "__main__":
    import html

    def naive(s: str) -> str:                      # грешното: първо водещата 0
        s = s.replace(" ", "")
        return "+359" + s[1:] if s.startswith("0") else s

    assert naive("00359888123456") == "+3590359888123456"
    for raw in ("0888 123 456", "+359 888 123 456", "00359888123456", "0888-123-456",
                "(0888) 123 456", "+359 888 123 456"):
        assert normalize_bg_mobile(raw, required=True) == "+359888123456", raw
    assert normalize_bg_mobile("+359 887 000 111", required=True) == "+359887000111"
    assert normalize_bg_mobile("0899 99 99 99", required=True) == "+359899999999"
    assert normalize_bg_mobile("0988 123 456", required=True) == "+359988123456"
    assert normalize_bg_mobile("", required=False) == "" and normalize_bg_mobile("   ", required=True) is None
    for bad in ("12345", "0888 123 45", "0888 123 4567", "00359 888 123 45",
                "+359 2 123 4567", "0359888123456", "+44 7700 900123", "0777 123 456",
                "0888 123 45a"):
        assert normalize_bg_mobile(bad, required=True) is None, bad
    assert normalize_bg_phone("02 981 23 45", required=True) == "+35929812345"
    assert normalize_bg_phone("+359 2 981 2345", required=True) == "+35929812345"
    assert normalize_bg_phone("032 123 456", required=True) == "+35932123456"
    assert normalize_bg_phone("00359888123456", required=True) == "+359888123456"
    assert normalize_bg_phone("", required=False) == ""
    for bad in ("12345", "0888 123 4567", "+44 7700 900123", "0359888123456", "02 981 23"):
        assert normalize_bg_phone(bad, required=True) is None, bad
    # Това прави markupsafe.escape (и Jinja за {{ }}) с въведеното.
    assert html.escape('<script>"x"</script>') == "&lt;script&gt;&quot;x&quot;&lt;/script&gt;"
    print("OK")
```
