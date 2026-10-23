"""
genesis_agent.code_edit — surgical, anchored edits to files that already exist.

Why this module exists at all: until now the only way the agent could change a
file was WRITE_FILE, which replaces the whole thing. That is fine for a file
the agent just authored, and actively destructive on someone else's 2000-line
module — the model has to reproduce the entire file from memory, and everything
it did not remember is silently gone. Nothing in the pipeline would notice: the
write succeeds, the tests fail somewhere unrelated, and the diff is unreadable.

So an edit here is anchored on text that must already be in the file:

    edit_file(path, old="def parse(s):", new="def parse(s: str) -> dict:")

and it refuses rather than guesses:

  * `old` not found                → error naming the closest lines it did find
  * `old` found more than once     → error listing every line, asking for more
                                     context (or an explicit replace_all)
  * the result stops parsing (.py) → NOT written; the file on disk is untouched

That last one is the point of doing this in code instead of in the prompt. A
model that produces a syntactically broken edit gets the parse error back and
tries again, while the file it was editing never spent a moment in a broken
state — which matters because the next thing the repair loop does is run the
project's real test suite, and a SyntaxError there looks like a completely
different bug.

Every successful edit returns a unified diff. The model reads its own change
back in the tool result rather than assuming what it did.
"""
from __future__ import annotations

import ast
import codecs
import difflib
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# Enough context to see the change without flooding the model's window.
_DIFF_CONTEXT = 3
# How many candidate lines to name when an anchor doesn't match.
_MAX_HINTS = 5


# ── Кодиране ────────────────────────────────────────────────────────────────
# Одит 2026-10-11: файл в cp1251 (обичаен за стари български проекти и
# Notepad-а на Windows) READ_FILE показваше като „�������“, EDIT_FILE падаше с
# „'utf-8' codec can't decode byte 0xc7“, а READ→WRITE заменяше на диска всяка
# кирилска буква с U+FFFD — безвъзвратно. Един latin-1 байт (`©` = 0xA9) в иначе
# UTF-8 .py файл го правеше нередактируем завинаги. Затова едно решение за
# всички инструменти: какво е кодирането и как се записва обратно БАЙТ ПО БАЙТ.
_BOMS = ((b"\x00\x00\xfe\xff", "utf-32-be"), (b"\xff\xfe\x00\x00", "utf-32-le"),
         (b"\xfe\xff", "utf-16-be"), (b"\xff\xfe", "utf-16-le"))
# При utf-8 и cp1251 непознатите байтове стават сурогати U+DC80–U+DCFF и при
# запис се връщат същите байтове (cp1251 има дупка на 0x98). cp1252 се избира
# само без дупките си, но и там записът е байт по байт.
_ESCAPED = ("utf-8", "cp1251", "cp1252")
_RAW_BYTE = re.compile("[\udc80-\udcff]")
_NON_ASCII = bytes(range(0x80))
_CYRILLIC_WORD = re.compile(rb"[\xc0-\xff]{3,}")


def _looks_cp1251(raw: bytes) -> bool:
    """Кирилицата в cp1251 е 0xC0–0xFF; в UTF-8 половината не-ASCII байтове са
    продължения 0x80–0xBF. Мерено 2026-10-11: български текст в cp1251 дава
    ≥90% байтове в 0xC0–0xFF, UTF-8 кирилица със случаен чужд байт — ~50%.

    Латиница в latin-1 също е 0xC0–0xFF (`é` = 0xE9), но буквите ѝ са сами
    между ASCII, а кирилицата е цели думи. Ревю 2026-10-11: `Café` ставаше
    „Cafй“. Мерено: в cp1251 81–100% от тези байтове са в поредици от ≥3 (без
    файл само с „Да“), в немски, френски, испански и португалски — 0%."""
    if b"\x00" in raw:
        return False
    high = raw.translate(None, _NON_ASCII)
    if not high:
        return False
    upper = high.translate(None, bytes(range(0x80, 0xC0)))
    if len(upper) / len(high) < 0.7:
        return False
    in_words = sum(len(m) for m in _CYRILLIC_WORD.findall(raw))
    return in_words / len(upper) >= 0.5


# ── cp1252 (Latin-1 плюс €, кавички и тирета) ─────────────────────────────
# Ревю 2026-10-15: „Café crème“ в cp1252 се виждаше като „Caf� cr�me“; ред с
# ударение не се редактираше (anchor-ът от READ_FILE носи U+FFFD, файлът —
# байта), нова „é“ влизаше като UTF-8 до latin-1 байтовете, READ→WRITE се
# отказваше. Mojibake идва от ГРЕШНО разпознаване, не от cp1252: UTF-8 с един
# чужд байт, прочетен като cp1252, дава „CafÃ©“, cp1251 — „Çäðàâåé“, cp1250 —
# „Pøíliš“; моделът копира това, а UTF-8 резервата на WRITE_FILE (знак извън
# cp1252) го заковава на диска. Затова cp1252 се избира само при ясни
# доказателства, иначе остава старото (UTF-8 + сурови байтове, U+FFFD).
_UTF8_SEQ = re.compile(rb"[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}|[\xf0-\xf4][\x80-\xbf]{3}")
# „Ã©“, „Â«“, „â€™“: UTF-8 за U+0080–U+FFFF. В cp1252 текст „Ã“/„Â“ пред
# пунктуация или малка буква с ударение пред два такива знака почти няма.
_UTF8_TELL = re.compile(rb"[\xc2\xc3][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}|[\xf0-\xf4][\x80-\xbf]{3}")
# Дума: латински букви и не-ASCII знаци (цифри, ASCII пунктуация и интервали делят).
_WORD = re.compile(r"[^\s!-@\[-`{-~]+")
_OPENERS = "“‘„‚«‹¿¡"
_CLOSERS = "”’»›…™®©°²³¹ªº‰†‡"
_INNER = "’‘·–—\xad´"   # „l’été“, „col·lecció“, „A–B“
# cp1250 (чешки, полски, унгарски, румънски, хърватски), прочетен като
# cp1252, дава букви, които никой западен език не смесва: ř→ø с ě→ì/ů→ù/č→è,
# č→è/ě→ì/ň→ò/ů→ù с á (италиански и каталунски нямат „á“), ő→õ с ö/ü/ű→û,
# ă→ã с ţ→þ, č→è с ć→æ/đ→ð, ń→ñ с ę→ê/ś→œ/ć→æ. Полските ą→¹, ł→³, ż→¿
# вътре в дума и румънското ş→º пред буква — също.
_CP1250_PAIRS = (("ø", "ìùèòïšžý"), ("ìùèò", "šžý"), ("èìòù", "á"), ("õ", "öüû"),
                 ("ã", "þ"), ("è", "æð"), ("ñ", "êœæ"), ("ð", "èšž"))
_CP1250_WORD = re.compile(r"[ºª][^\W\d_]")
_SAMPLE_WORDS = 2000


def _accented_words(text: str) -> Iterator[str]:
    """Думите с не-ASCII буква, без кавичките около тях — първите
    _SAMPLE_WORDS: латински файл от 2 MB иначе струваше 0.6 s (мерено
    2026-10-15), а толкова думи решават еднакво."""
    n = 0
    for line in text.split("\n"):
        if line.isascii():
            continue
        for m in _WORD.finditer(line):
            core = m.group().lstrip(_OPENERS).rstrip(_CLOSERS)
            if any(not c.isascii() and c.isalpha() for c in core):
                yield core
                n += 1
                if n >= _SAMPLE_WORDS:
                    return


def _looks_cp1252(raw: bytes) -> bool:
    """Западноевропейски текст в cp1252 — само ако е ясно (иначе False).

    Кирилицата в cp1251 и буквите с ударение в cp1252 са едни и същи байтове
    0xC0–0xFF; различава ги мястото им. Ударената латинска буква стои в дума
    с ASCII букви („Café“, „Müller“, „São“), кирилската дума е цяла от високи
    байтове („Да“ = „Äà“). Една буква сама („é“ = „и“, „à“ = „а“) не решава
    нищо; знак без буква („©“, „€“, „—“) — също.

    Мерено 2026-10-15 (думи и абзаци на 13 западни езика, 7 централно-
    европейски, български, руски, украински, сръбски; всяка проба и с CRLF
    минава decode→encode байт по байт): cp1251 — 0 от 29 като cp1252;
    cp1252 — 52 от 65 разпознати, останалите 13 (една буква сама, само
    знаци, Fuß в кавички — „ß“ и затварящата кавичка са валиден UTF-8)
    остават U+FFFD; UTF-8 с
    чужд байт — 0 от 7 като cp1252; cp1250 — 17 от 27 остават U+FFFD, 3 се
    четат вярно (общите букви), 7 кратки с една-единствена различна буква се
    виждат с двойничка („řeka“ → „øeka“, „őszi“ → „õszi“).
    """
    if b"\x00" in raw:
        return False
    high = len(raw.translate(None, _NON_ASCII))
    if not high or _UTF8_TELL.search(raw):
        return False
    # Повечето високи байтове са валиден UTF-8: това е UTF-8 с чужд байт.
    if sum(len(m) for m in _UTF8_SEQ.findall(raw)) * 2 >= high:
        return False
    try:
        text = raw.decode("cp1252")   # 0x81, 0x8D, 0x8F, 0x90, 0x9D: не е cp1252
    except UnicodeDecodeError:
        return False
    latin = cyrillic = 0
    letters: set[str] = set()
    for core in _accented_words(text):
        accents = [c for c in core if not c.isascii()]
        if any(not c.isalpha() and c not in _INNER for c in accents) or _CP1250_WORD.search(core):
            return False
        if any(c.isascii() and c.isalpha() for c in core):
            latin += 1
            letters.update(c.lower() for c in accents if c.isalpha())
        elif len(accents) >= 2:
            cyrillic += 1
    if any(letters & set(a) and letters & set(b) for a, b in _CP1250_PAIRS):
        return False
    return latin >= 1 and cyrillic * 4 <= latin


def decode_text(raw: bytes, *, partial: bool = False) -> tuple[str, str]:
    """(текст, кодиране) — `encode_text(текст, кодиране) == raw` винаги.

    BOM-ът (UTF-8 или UTF-16/32) остава в текста като U+FEFF, за да се запише
    обратно. `partial=True`: `raw` е само началото на файла — недовършена
    последователност в края не е грешка в кодирането.
    """
    for bom, enc in _BOMS:
        if raw.startswith(bom):
            try:
                return codecs.getincrementaldecoder(enc)().decode(raw, final=not partial), enc
            except UnicodeDecodeError:
                break
    try:
        return codecs.getincrementaldecoder("utf-8")().decode(raw, final=not partial), "utf-8"
    except UnicodeDecodeError:
        pass
    if _looks_cp1251(raw):
        return raw.decode("cp1251", "surrogateescape"), "cp1251"
    # След cp1251: българският файл си остава какъвто беше преди cp1252.
    if _looks_cp1252(raw):
        return raw.decode("cp1252", "surrogateescape"), "cp1252"
    return raw.decode("utf-8", "surrogateescape"), "utf-8"


def encode_text(text: str, encoding: str) -> bytes:
    """Обратното на decode_text. UnicodeEncodeError, ако знак няма място в
    кодирането (емоджи в cp1251) — извикващият отказва записа."""
    return text.encode(encoding, "surrogateescape" if encoding in _ESCAPED else "strict")


def has_raw_bytes(text: str) -> bool:
    """Има ли байтове, които не са текст в разпознатото кодиране."""
    return bool(_RAW_BYTE.search(text))


def displayable(text: str) -> str:
    """Самотните сурогати не стигат до модела: чупят UTF-8 на JSON заявката."""
    return _RAW_BYTE.sub("\ufffd", text)


def read_text_any(path: str | Path) -> str:
    """Текстът на файл за ЧЕТЕНЕ (не за запис обратно): без BOM, без сурогати.

    Одит 2026-10-11: requirements.txt от `pip freeze >` в PowerShell 5.1 е
    UTF-16 и export отчиташе „��r e q u e s t s“ като зависимост, а обявените
    пакети — за липсващи; BOM пред pyproject.toml пък чупеше tomllib. pip
    чете и двата файла без проблем.
    """
    return displayable(decode_text(Path(path).read_bytes())[0]).removeprefix("\ufeff")


@dataclass
class EditResult:
    ok: bool
    detail: str
    diff: str = ""
    replacements: int = 0
    lines_changed: tuple[int, int] = field(default=(0, 0))  # (added, removed)


def _unified_diff(before: str, after: str, name: str) -> str:
    lines = list(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}",
        n=_DIFF_CONTEXT,
    ))
    # Последният ред без `\n` (одит 2026-10-22): „-two“ и „+three“ се слепваха
    # в „-two+three“, а броят излизаше +0/-1 — моделът четеше дифф, който не
    # показва промяната му. Маркерът е този на git и patch.
    return "".join(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n"
                   for ln in lines)


def _count_changed(diff: str) -> tuple[int, int]:
    added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
    return added, removed


def _match_line_endings(haystack: str, old: str, new: str) -> tuple[str, str]:
    """Привежда anchor-а (и заместителя) към краищата, с които е записан файлът.

    Моделът почти винаги пише `\n`, дори когато е цитирал CRLF файл. Без това
    anchor-ът просто нямаше да съвпадне и редакцията щеше да бъде отказана с
    подвеждащото "няма такъв текст". `new` минава през същото привеждане, за
    да не се получи `\r\r\n` от вече CRLF заместител.
    """
    flat_old = old.replace("\r\n", "\n").replace("\r", "\n")
    flat_new = new.replace("\r\n", "\n").replace("\r", "\n")

    if "\n" in old:
        # Многоредов anchor: опитваме както е, после с CRLF.
        if flat_old in haystack:
            return flat_old, flat_new
        crlf_old = flat_old.replace("\n", "\r\n")
        if crlf_old in haystack:
            return crlf_old, flat_new.replace("\n", "\r\n")
        return flat_old, flat_new

    # Едноредов anchor, но `new` може да е многоредов и да носи CRLF —
    # ранното връщане тук вкарваше `\r` в чист LF файл (хванато от
    # tests/test_code_edit_invariants.py, не на око). Нямаме локален контекст
    # за стила, затова следваме файла; при смесен файл LF е по-безопасният
    # избор, защото не въвежда CR там, където го е нямало.
    if "\n" not in flat_new:
        return flat_old, flat_new
    crlf_count = haystack.count("\r\n")
    lf_only = haystack.count("\n") - crlf_count
    if crlf_count and not lf_only:
        return flat_old, flat_new.replace("\n", "\r\n")
    return flat_old, flat_new


def _occurrence_lines(text: str, needle: str) -> list[int]:
    """1-indexed line numbers where `needle` starts."""
    lines: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            return lines
        lines.append(text.count("\n", 0, idx) + 1)
        start = idx + 1


def _near_misses(text: str, needle: str) -> str:
    """
    An anchor that doesn't match is nearly always whitespace or a stale copy of
    the line, so pointing at the closest real lines saves a round trip that
    would otherwise be spent re-reading the whole file.
    """
    first = (needle.strip().splitlines() or [""])[0].strip()
    if len(first) < 4:
        return ""
    hits = [
        f"  L{i}: {ln.rstrip()}"
        for i, ln in enumerate(text.splitlines(), 1)
        if first in ln
    ]
    if not hits:
        # Fall back to fuzzy matching on the first line — catches renamed
        # identifiers and changed indentation, which exact search cannot.
        candidates = {ln.strip(): i for i, ln in enumerate(text.splitlines(), 1) if ln.strip()}
        close = difflib.get_close_matches(first, list(candidates), n=_MAX_HINTS, cutoff=0.6)
        hits = [f"  L{candidates[c]}: {c}" for c in close]
    if not hits:
        return ""
    shown = hits[:_MAX_HINTS]
    more = "" if len(hits) <= _MAX_HINTS else f"\n  … и още {len(hits) - _MAX_HINTS}"
    return "\nНай-близките редове във файла:\n" + "\n".join(shown) + more


# b'…' с кирилица вътре — ast.parse казва само първия ред, а тестове за
# български текст го пишат на всеки assert (`assert b'Мария' in resp.data`).
_NON_ASCII_BYTES = re.compile(r"""(?<![\w'"])[bB][rR]?(['"])(?:(?!\1).)*?[^\x00-\x7f]""")


def syntax_error(path: Path, text: str) -> str:
    """Empty string when `text` parses (or `path` is not Python)."""
    if path.suffix != ".py":
        return ""
    try:
        # UTF-8 BOM (Notepad на Windows): Python го приема във файл, ast.parse в
        # низ — не; без това всяка редакция се отказваше като „чупи синтаксиса“.
        ast.parse(displayable(text).removeprefix("\ufeff"))
    except SyntaxError as e:
        msg = f"{e.msg} (ред {e.lineno})"
        if "bytes can only contain ASCII" in (e.msg or ""):
            lines = [i for i, ln in enumerate(text.splitlines(), 1)
                     if _NON_ASCII_BYTES.search(ln)]
            where = ", ".join(f"L{n}" for n in lines[:_MAX_HINTS * 4])
            msg += (f". Такива b'…' с кирилица има на редове: {where}. "
                    "За текст ползвай str: 'Мария' in resp.get_data(as_text=True) "
                    "или 'Мария'.encode() in resp.data")
        return msg
    return ""


def edit_file(path: str | Path, old: str, new: str, *,
              replace_all: bool = False) -> EditResult:
    """
    Replace `old` with `new` in `path`. See the module docstring for the
    refusal rules — every one of them leaves the file exactly as it was.
    """
    p = Path(path)
    if not old:
        return EditResult(False, "Празен anchor: 'old' трябва да е точен текст, "
                                 "който вече съществува във файла.")
    try:
        # Работим върху СУРОВИЯ текст — без universal newlines и без
        # нормализация. `read_text` връща вече преобразуван текст и записът
        # после слагаше LF навсякъде: един редактиран ред пренаписваше
        # краищата на ЦЕЛИЯ файл.
        #
        # Първият опит за фикс (запомни стила, наложи го при запис) поправяше
        # чистите CRLF файлове и чупеше смесените: файл с `\r\n` И `\n`
        # (обичайно за репо, редактирано и на двете платформи) излизаше изцяло
        # CRLF — същата повреда, само за друг вид файл. Няма как да
        # "възстановиш стила" на файл, който няма един стил.
        #
        # Затова: заменя се ТОЧНО намереното парче, а всичко останало се
        # пренася байт по байт. Anchor-ът се привежда към краищата, с които
        # реално е записан файлът (моделът почти винаги пише `\n`), и `new`
        # получава същото третиране — иначе CRLF в new се удвояваше до
        # `\r\r\n`.
        #
        # Кодирането се пази по същата логика (одит 2026-10-11): cp1251 или
        # чужд байт в UTF-8 се записват обратно точно както са били.
        #
        # Само обикновен файл (одит 2026-10-22): върху FIFO read_bytes() чакаше
        # вечно (мерено — нишката висеше над 5 s), точно като READ_FILE до
        # 2026-10-11; затова и същият отказ.
        st = os.stat(p)
        if stat.S_ISDIR(st.st_mode):
            return EditResult(False, f"{p} е папка, не файл.")
        if not stat.S_ISREG(st.st_mode):
            return EditResult(False, f"{p} не е обикновен файл (FIFO, устройство или сокет) — "
                                     "не се редактира: четенето би чакало вечно или нямало край.")
        before, encoding = decode_text(p.read_bytes())
    except FileNotFoundError:
        return EditResult(False, f"Файлът не съществува: {p} "
                                 "(за нов файл ползвай WRITE_FILE)")
    except OSError as e:
        return EditResult(False, f"Грешка при четене: {e}")

    old, new = _match_line_endings(before, old, new)
    hits = _occurrence_lines(before, old)
    if not hits:
        return EditResult(False,
                          f"Anchor-ът не е намерен в {p.name}. Провери точния текст "
                          f"(интервали и отстъпи също се броят)."
                          + displayable(_near_misses(before, old)))
    if len(hits) > 1 and not replace_all:
        where = ", ".join(f"L{n}" for n in hits[:_MAX_HINTS])
        return EditResult(False,
                          f"Anchor-ът се среща {len(hits)} пъти в {p.name} ({where}). "
                          "Добави повече контекст, за да е уникален, или ползвай "
                          "replace_all=true, ако наистина искаш всички.")

    if old == new:
        return EditResult(False, "'old' и 'new' са идентични — няма промяна.")

    after = before.replace(old, new) if replace_all else before.replace(old, new, 1)

    err = syntax_error(p, after)
    still = ""
    if err:
        if not syntax_error(p, before):
            return EditResult(False,
                              f"Промяната чупи синтаксиса на {p.name}: {err}. "
                              "ФАЙЛЪТ НЕ Е ПРОМЕНЕН — поправи редакцията и опитай пак.")
        # Файлът беше счупен ОЩЕ ПРЕДИ редакцията. Отказът тук нищо не пази, а
        # заключва поправката: с две грешки всяка редакция, която оправя едната,
        # „чупи синтаксиса“ заради другата (bench booking-form, 2026-10-01: шест
        # отказа подред, ходът изгоря на тавана от 25 рунда).
        still = f"\n⚠ {p.name} още не се компилира: {err}"

    try:
        data = encode_text(after, encoding)
    except UnicodeEncodeError as e:
        return EditResult(False,
                          f"{p.name} е в кодиране {encoding} и знакът {after[e.start:e.end]!r} "
                          "няма място в него. ФАЙЛЪТ НЕ Е ПРОМЕНЕН — ползвай знак, който "
                          "кодирането има.")
    diff = displayable(_unified_diff(before, after, p.name))
    try:
        # Байтове, не текстов режим: Python не преобразува нищо при запис —
        # извън заменения участък файлът излиза точно както е влязъл.
        p.write_bytes(data)
    except OSError as e:
        return EditResult(False, f"Грешка при запис: {e}")

    added, removed = _count_changed(diff)
    # Колко замени реално направи str.replace — без застъпване (одит 2026-10-22):
    # „aa“ в „aaa“ се отчиташе като 2 замени при една. `hits` нарочно брои и
    # застъпените: такъв anchor без replace_all е двусмислен и се отказва.
    n = before.count(old) if replace_all else 1
    return EditResult(
        True,
        f"✓ {p} — {n} замяна(и), +{added}/-{removed} реда"
        + ("" if encoding == "utf-8" else f" · кодиране {encoding} (запазено)") + still,
        diff=diff, replacements=n, lines_changed=(added, removed),
    )
