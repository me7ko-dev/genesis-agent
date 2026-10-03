---
name: bg_contact_form_phone
category: domain
min_score: 3
description: Български мобилен номер във форма — 0888 123 456, +359 888 123 456 и 00359888123456
  → +359888123456 (00359 преди 0), кодове 87/88/89/98/99 и 9 цифри; Flask 3 — escape от markupsafe,
  POST → 303 точно към поискания път.
triggers:
- български мобилен номер
- телефон +359 00359
- мобилен телефон номер
- форма за запитване flask
version: '1.0'
author: Genesis
last_updated: '2026-10-04T02:30:00+00:00'
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
"""Български мобилен номер → +359XXXXXXXXX; Flask 3 форма без капаните.

Факти:
- Записите на един номер: 0888 123 456 · 0888-123-456 · +359 888 123 456 · 00359888123456
  · (0888) 123 456. Махни интервалите (и неразделимия U+00A0), -, ., /, ( и ).
- Редът на префиксите: "+359", после "00359", ЧАК ТОГАВА "0". Ако първо махнеш водещата
  "0", 00359888123456 става +3590359888123456 — грешката, измерена два пъти.
- След префикса остават ТОЧНО 9 цифри с код 87/88/89/98/99 (КРС). 8 цифри, 10 цифри,
  стационарен (+359 2 …) или чужд номер → невалиден.
- Празно поле, когато телефонът не е задължителен, е "" — не грешка.
- Flask 3.0 махна flask.escape и flask.Markup: `from markupsafe import escape`.
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


def normalize_bg_mobile(raw: str | None) -> str | None:
    """'0888 123 456' / '+359 888 123 456' / '00359888123456' → '+359888123456';
    '' → ''; невалиден → None."""
    s = _SEPARATORS.sub("", raw or "")
    if not s:
        return ""
    for prefix in ("+359", "00359", "0"):
        if s.startswith(prefix):
            rest = s[len(prefix):]
            return "+359" + rest if _MOBILE.fullmatch(rest) else None
    return None


if __name__ == "__main__":
    import html

    def naive(s: str) -> str:                      # грешното: първо водещата 0
        s = s.replace(" ", "")
        return "+359" + s[1:] if s.startswith("0") else s

    assert naive("00359888123456") == "+3590359888123456"
    for raw in ("0888 123 456", "+359 888 123 456", "00359888123456", "0888-123-456",
                "(0888) 123 456", "+359 888 123 456"):
        assert normalize_bg_mobile(raw) == "+359888123456", raw
    assert normalize_bg_mobile("+359 887 000 111") == "+359887000111"
    assert normalize_bg_mobile("0899 99 99 99") == "+359899999999"
    assert normalize_bg_mobile("0988 123 456") == "+359988123456"
    assert normalize_bg_mobile("") == "" and normalize_bg_mobile("   ") == ""
    for bad in ("12345", "0888 123 45", "0888 123 4567", "00359 888 123 45",
                "+359 2 123 4567", "0359888123456", "+44 7700 900123", "0777 123 456",
                "0888 123 45a"):
        assert normalize_bg_mobile(bad) is None, bad
    # Това прави markupsafe.escape (и Jinja за {{ }}) с въведеното.
    assert html.escape('<script>"x"</script>') == "&lt;script&gt;&quot;x&quot;&lt;/script&gt;"
    print("OK")
```
