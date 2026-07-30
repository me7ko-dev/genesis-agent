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
import difflib
from dataclasses import dataclass, field
from pathlib import Path

# Enough context to see the change without flooding the model's window.
_DIFF_CONTEXT = 3
# How many candidate lines to name when an anchor doesn't match.
_MAX_HINTS = 5


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
        ast.parse(text)
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
        before = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EditResult(False, f"Файлът не съществува: {p} "
                                 "(за нов файл ползвай WRITE_FILE)")
    except OSError as e:
        return EditResult(False, f"Грешка при четене: {e}")

    hits = _occurrence_lines(before, old)
    if not hits:
        return EditResult(False,
                          f"Anchor-ът не е намерен в {p.name}. Провери точния текст "
                          f"(интервали и отстъпи също се броят)." + _near_misses(before, old))
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

    diff = _unified_diff(before, after, p.name)
    try:
        p.write_text(after, encoding="utf-8")
    except OSError as e:
        return EditResult(False, f"Грешка при запис: {e}")

    added, removed = _count_changed(diff)
    n = len(hits) if replace_all else 1
    return EditResult(
        True,
        f"✓ {p} — {n} замяна(и), +{added}/-{removed} реда",
        diff=diff, replacements=n, lines_changed=(added, removed),
    )
