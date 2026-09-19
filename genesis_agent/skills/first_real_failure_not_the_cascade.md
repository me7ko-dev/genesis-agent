---
name: 'first_real_failure_not_the_cascade'
category: 'autonomous'
description: 'From pytest/ruff/mypy output, identify the FIRST REAL failure to fix — the root cause — rather than the last red line on screen, which is usually a downstream consequence (a broken import knocks out thirty tests).'
triggers: ["first real failure not the cascade"]
version: '1.0'
author: 'Genesis'
last_updated: '2026-09-19T23:00:09.199511+00:00'
---

## Описание
From pytest/ruff/mypy output, identify the FIRST REAL failure to fix — the root cause — rather than the last red line on screen, which is usually a downstream consequence (a broken import knocks out thirty tests).

## Python Код
```python
"""Намира ПЪРВИЯ истински провал в изход от тестове/линтър, не каскадните следствия.

Защо съществува: агентът редовно се хваща за последното червено нещо на
екрана и започва да го "поправя". Последното обаче почти винаги е СЛЕДСТВИЕ.
Счупен импорт събаря тридесет теста; тридесетте FAILED реда са шум, коренът е
единственият collection error най-горе. Поправяш ли следствието, счупваш и
кода, и следата към причината.

Редът на приоритет тук не е естетика, а причинност:
    1. collection/import грешка  — нищо под нея не е било изпълнено изобщо;
    2. syntax грешка             — същото, но откъм линтъра/компилатора;
    3. първият провален тест     — по ред на изпълнение, не последният;
    4. първата lint/type находка — те са независими, затова е просто първата.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Всеки образец: (вид, тежест, регекс). По-малка тежест = по-близо до корена.
_PATTERNS: tuple[tuple[str, int, re.Pattern[str]], ...] = (
    ("collection_error", 0, re.compile(
        r"^(?:ERROR|E)\s+(?P<where>\S+).*?(?:ImportError|ModuleNotFoundError|"
        r"CollectionError|error during collection)", re.MULTILINE)),
    # Водещото `E ` е pytest-овият префикс на traceback редове. Без него
    # `E   ModuleNotFoundError: ...` даваше where="E" — уверено, но безполезно
    # място, което праща агента да поправя файл на име "E". Хванато само
    # защото скилът беше пуснат срещу ИСТИНСКИ pytest изход; измисленият
    # пример нямаше този префикс.
    ("import_error", 0, re.compile(
        r"^(?:E\s+)?(?P<where>[^\s:]*?):?\s*(?:ModuleNotFoundError|ImportError):"
        r"\s*(?P<msg>.+)$", re.MULTILINE)),
    ("syntax_error", 1, re.compile(
        r"^(?:\s*File \"(?P<where>[^\"]+)\", line (?P<line>\d+).*?\n)?.*?"
        r"(?P<msg>SyntaxError:.+)$", re.MULTILINE)),
    ("test_failure", 2, re.compile(
        r"^FAILED\s+(?P<where>\S+?)(?:\s+-\s+(?P<msg>.+))?$", re.MULTILINE)),
    ("test_error", 2, re.compile(
        r"^ERROR\s+(?P<where>\S+?)(?:\s+-\s+(?P<msg>.+))?$", re.MULTILINE)),
    ("type_error", 3, re.compile(
        r"^(?P<where>[^\s:]+):(?P<line>\d+):(?:\d+:)?\s*error:\s*(?P<msg>.+)$",
        re.MULTILINE)),
    ("lint_finding", 3, re.compile(
        r"^(?P<code>[A-Z]+\d+)\s+(?P<msg>.+?)\n\s*-->\s*(?P<where>[^\s:]+):(?P<line>\d+)",
        re.MULTILINE)),
)


_LOCATION_RE = re.compile(r"^\s*(?:File \"(?P<f1>[^\"]+)\", line (?P<l1>\d+)"
                          r"|(?P<f2>[^\s:\"]+\.\w+):(?P<l2>\d+))", re.MULTILINE)


def _nearest_location(output: str, before: int) -> tuple[str, str] | None:
    """Последният път:линия ПРЕДИ дадена позиция — най-близкият контекст."""
    last = None
    for m in _LOCATION_RE.finditer(output, 0, before):
        last = (m.group("f1") or m.group("f2"), m.group("l1") or m.group("l2"))
    return last


@dataclass(frozen=True)
class Failure:
    kind: str
    where: str
    line: int | None
    message: str
    why_first: str


def first_real_failure(output: str) -> Failure | None:
    """Кой провал да се поправи ПЪРВИ. None = нищо провалено не е разпознато.

    Връща и `why_first` — агентът трябва да може да обясни защо пренебрегва
    останалите тридесет реда, иначе изглежда сякаш ги е пропуснал.
    """
    if not output or not output.strip():
        return None

    best: tuple[int, int, Failure] | None = None
    for kind, severity, pattern in _PATTERNS:
        match = pattern.search(output)
        if not match:
            continue
        groups = match.groupdict()
        line_raw = groups.get("line")
        where = (groups.get("where") or "").strip()
        if not where:
            # pytest слага пътя на РЕДА НАД грешката ("tests/x.py:48: in <module>"),
            # не на самия E ред. Празно "място" при наличен път в изхода е
            # безполезно за агента, затова го вземаме отгоре.
            where, line_raw = _nearest_location(output, match.start()) or ("", line_raw)
        failure = Failure(
            kind=kind,
            where=where or "(неизвестно място)",
            line=int(line_raw) if line_raw and str(line_raw).isdigit() else None,
            message=(groups.get("msg") or groups.get("code") or match.group(0)).strip()[:300],
            why_first=_WHY[kind],
        )
        candidate = (severity, match.start(), failure)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return best[2] if best else None


_WHY = {
    "collection_error": "Тестовете дори не са стартирали — всичко под това е следствие.",
    "import_error": "Счупен импорт; всеки провал, който го ползва, е следствие.",
    "syntax_error": "Файлът не се парсва — нищо в него не е било изпълнено.",
    "test_failure": "Първият провалил се тест по ред на изпълнение, не последният на екрана.",
    "test_error": "Първата грешка по ред на изпълнение.",
    "type_error": "Първата типова находка; останалите може да са следствие от нея.",
    "lint_finding": "Първата lint находка.",
}


def summarize(failure: Failure | None) -> str:
    """Един абзац за човека/агента — какво да поправи и защо точно него."""
    if failure is None:
        return "Не разпознах провал в този изход."
    place = failure.where + (f":{failure.line}" if failure.line else "")
    return (f"Поправи ПЪРВО: {place}\n"
            f"  {failure.kind}: {failure.message}\n"
            f"  Защо то: {failure.why_first}")


if __name__ == "__main__":
    # Каскадата: счупен импорт събаря много тестове. Коренът е импортът,
    # а не тридесетте FAILED реда под него.
    cascade = """
ImportError while importing test module '/repo/tests/test_api.py'.
tests/test_api.py:3: in <module>
    from app.models import User
E   ModuleNotFoundError: No module named 'app.models'
FAILED tests/test_a.py::test_one - AttributeError
FAILED tests/test_b.py::test_two - AttributeError
FAILED tests/test_c.py::test_three - AttributeError
"""
    got = first_real_failure(cascade)
    assert got is not None
    assert got.kind in ("collection_error", "import_error"), got.kind
    assert "app.models" in got.message, got.message
    assert "test_a.py" not in got.where, "следствието не бива да се води корен"

    # Истинският pytest формат: "E   ImportError: ..." — префиксът E НЕ е файл.
    pytest_real = """
tests/test_gui.py:48: in <module>
    gi = pytest.importorskip("gi")
E   ImportError: cannot import name '_gi' from partially initialized module 'gi'
"""
    got = first_real_failure(pytest_real)
    assert got is not None and got.kind == "import_error"
    assert got.where != "E", "pytest-овият E префикс не е име на файл"
    assert "_gi" in got.message
    # Пътят стои един ред по-горе — да се каже "неизвестно място", когато го
    # има в изхода, е безполезно за агента, който трябва да го отвори.
    assert got.where == "tests/test_gui.py", got.where
    assert got.line == 48, got.line

    # Без каскада: първият провален тест, не последният на екрана.
    plain = """
FAILED tests/test_first.py::test_alpha - assert 1 == 2
FAILED tests/test_second.py::test_beta - assert 3 == 4
"""
    got = first_real_failure(plain)
    assert got is not None and "test_first.py" in got.where, got.where

    # Синтактичната грешка бие провалените тестове.
    mixed = """
FAILED tests/test_x.py::test_y - assert False
  File "app/broken.py", line 12
    def f(:
SyntaxError: invalid syntax
"""
    got = first_real_failure(mixed)
    assert got is not None and got.kind == "syntax_error", got.kind

    # mypy формат.
    got = first_real_failure("app/core.py:42: error: Incompatible return value type")
    assert got is not None and got.kind == "type_error"
    assert got.where == "app/core.py" and got.line == 42

    # ruff формат.
    ruff_out = 'F401 `os` imported but unused\n  --> app/util.py:3\n'
    got = first_real_failure(ruff_out)
    assert got is not None and got.kind == "lint_finding", got.kind
    assert got.where == "app/util.py" and got.line == 3

    # Чист изход и празен вход не измислят провал.
    assert first_real_failure("42 passed in 1.2s") is None
    assert first_real_failure("") is None
    assert first_real_failure("   \n  ") is None

    # Обяснението винаги придружава находката — иначе изглежда, че
    # останалите редове просто са пропуснати.
    assert "следствие" in summarize(first_real_failure(cascade))

    print("OK")
```

## Pitfalls
- OK
