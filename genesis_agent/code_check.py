"""Python written in this turn gets tried beyond the task's own examples.

bench_fcc 2026-09-29: once a task arrived whole, most answers were ONE
WRITE_FILE and "done" — 25 s instead of 300, but two of twenty came back 3/5.
One was never run; the other was run with exactly the two examples in the
task, both passed, and the bug was in deeper nesting than they had. When the
model ends its turn after writing .py, it is told once: run it (if it did
not), and try a case per rule — harder input, the edges, input to reject.

bench_fcc 2026-09-30: 84 of 90 such checks were print() only. One printed
"Test 9: 3 expected 4", then "All tests passed!", and the model said done —
the answer failed 4 of 6 hidden tests. So each case is an assert: a mismatch
ends the command with rc≠0 instead of scrolling past.
"""
from __future__ import annotations

import re
from pathlib import Path

_WRITTEN = re.compile(r"^\[(?:WRITE_FILE|EDIT_FILE): ([^\]\n]+)\] ✓", re.MULTILINE)
_RUN = re.compile(r"^\[RUN_CMD: ", re.MULTILINE)
# A command word, not ".py" at the end of a file name.
_PYTHON_CMD = re.compile(r"""(?:^|[\s"'/\\&|;(])(?:python3?|py|pytest)(?:\.exe)?(?=[\s"']|$)""",
                         re.IGNORECASE | re.MULTILINE)
_CASES = ("по един случай за всяко правило или ограничение в условието, различен от примерите "
          "в заявката: по-сложен вход, граничните стойности и вход, който трябва да бъде "
          "отхвърлен. Всеки случай е `assert резултат == очаквано, (вход, резултат)`, не print — "
          "отпечатано „очаквано 4“ до 3 никой не сравнява, а падащ assert дава rc≠0. Очакваното "
          "смятай от условието, не от кода")


class RunCheck:
    """The agent loop calls `observe` for every tool result and `note()` when
    the model stops calling tools; a non-empty note goes back to the model."""

    def __init__(self) -> None:
        self._written: dict[str, Path] = {}
        self._unrun: set[str] = set()
        self._nudged = False

    def observe(self, result: str) -> None:
        result = result or ""
        for p in _WRITTEN.findall(result):
            path = Path(p.strip())
            if path.suffix.lower() == ".py":
                self._written[path.name] = path
                self._unrun.add(path.name)
        command = result.split("]  (rc=", 1)[0].removeprefix("[RUN_CMD: ")
        if self._unrun and _RUN.match(result) and _PYTHON_CMD.search(command):
            self._unrun.clear()

    def due(self) -> bool:
        return bool(self._written) and not self._nudged

    def note(self) -> str:
        if not self.due():
            return ""
        self._nudged = True
        if self._unrun:
            names = ", ".join(sorted(self._unrun))
            return (f"[проверка на кода] {names}: записан(и), но НЕ пуснат(и) след последната "
                    f"промяна. Преди да кажеш „готово“ — пусни кода (RUN_CMD) с примерите от "
                    f"заявката и с {_CASES}. Разминаване → поправи и пусни пак. Ако кодът "
                    "наистина не може да се пусне тук, кажи защо.")
        return ("[проверка на кода] Преди „готово“: примерите от заявката не доказват правилата. "
                f"Пусни кода с {_CASES}. Разминаване → поправи и пусни пак. Ако вече си ги "
                "пробвал с assert — кажи в един ред кои и приключи.")
