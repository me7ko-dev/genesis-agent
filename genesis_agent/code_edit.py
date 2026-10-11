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
import re
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
# запис се връщат същите байтове (cp1251 има дупка на 0x98).
_ESCAPED = ("utf-8", "cp1251")
_RAW_BYTE = re.compile("[\udc80-\udcff]")
_NON_ASCII = bytes(range(0x80))


def _looks_cp1251(raw: bytes) -> bool:
    """Кирилицата в cp1251 е 0xC0–0xFF; в UTF-8 половината не-ASCII байтове са
    продължения 0x80–0xBF. Мерено 2026-10-11: български текст в cp1251 дава
    ≥90% байтове в 0xC0–0xFF, UTF-8 кирилица със случаен чужд байт — ~50%."""
    if b"\x00" in raw:
        return False
    high = raw.translate(None, _NON_ASCII)
    if not high:
        return False
    upper = high.translate(None, bytes(range(0x80, 0xC0)))
    return len(upper) / len(high) >= 0.7


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
    return "".join(lines)


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


def _syntax_error(path: Path, text: str) -> str:
    """Empty string when the result is fine to write."""
    if path.suffix != ".py":
        return ""
    try:
        # UTF-8 BOM (Notepad на Windows): Python го приема във файл, ast.parse в
        # низ — не; без това всяка редакция се отказваше като „чупи синтаксиса“.
        ast.parse(displayable(text).removeprefix("\ufeff"))
    except SyntaxError as e:
        return f"{e.msg} (ред {e.lineno})"
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

    err = _syntax_error(p, after)
    if err:
        return EditResult(False,
                          f"Промяната чупи синтаксиса на {p.name}: {err}. "
                          "ФАЙЛЪТ НЕ Е ПРОМЕНЕН — поправи редакцията и опитай пак.")

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
    n = len(hits) if replace_all else 1
    return EditResult(
        True,
        f"✓ {p} — {n} замяна(и), +{added}/-{removed} реда"
        + ("" if encoding == "utf-8" else f" · кодиране {encoding} (запазено)"),
        diff=diff, replacements=n, lines_changed=(added, removed),
    )
