"""Python written in this turn gets tried beyond the task's own examples.

bench_fcc 2026-09-29: once a task arrived whole, most answers were ONE
WRITE_FILE and "done" — 25 s instead of 300, but two of twenty came back 3/5.
One was never run; the other was run with exactly the two examples in the
task, both passed, and the bug was in deeper nesting than they had. When the
model ends its turn after writing .py, it is told once: run it (if it did
not), and try a case per rule — harder input, the edges, input to reject.
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
          "отхвърлен")
# bench_fcc 2026-09-30: c333 — собствен грешен assert, верен код, и моделът
# „поправи“ ВЕРНИЯ код по assert-а; c125 — обратното, „коригира“ теста.
_MISMATCH = ("Разминаване → първо провери очакваното срещу условието (не срещу своята догадка): "
             "грешното очакване се поправя в проверката, кодът се пипа само ако условието го "
             "иска. После пусни пак.")
# NEXT_STEPS Б.6: „ОБЩО със сумата“ — число или речник? Тълкуването не се крие.
_ASSUMPTIONS = ("Ако условието допуска две тълкувания, избери едното и запиши допускането в "
                "README.md и в отговора.")
# bench sklad-package 2026-10-02: „ред ОБЩО“, а отчетът печата TOTAL 4/5 пъти (Claude —
# ОБЩО 2/2 със същата подкана). Само думата след ред/ключ/колона/надпис и текстът в
# кавички след →/печата/показва — не всяка главна дума (НАГОРЕ, ПАПКА, БУЛСТАТ).
_NAMED = re.compile(r"(?:\bред|\bключ|\bколона|\bнадпис|\bзаглавие)\s+[„\"«]?([А-ЯA-Z]{3,})\b")
_SHOWN = re.compile(r"(?:→|\bпечата|\bпоказва|\bизвежда|\bсъобщение)\s*[„\"«]([^“\"»\n]{2,60})[“\"»]")


def literals(task: str) -> list[str]:
    """The strings the task wants shown verbatim, in their order."""
    found = sorted((m.start(1), m.group(1)) for rx in (_NAMED, _SHOWN)
                    for m in rx.finditer(task or ""))
    return list(dict.fromkeys(s for _, s in found))


class RunCheck:
    """The agent loop calls `observe` for every tool result and `note()` when
    the model stops calling tools; a non-empty note goes back to the model."""

    def __init__(self, task: str = "") -> None:
        self._written: dict[str, Path] = {}
        self._unrun: set[str] = set()
        self._nudged = False
        self._literals = literals(task)

    def _missing_literals(self) -> str:
        texts = []
        for path in self._written.values():
            try:
                texts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        missing = [s for s in self._literals if texts and not any(s in t for t in texts)]
        if not missing:
            return ""
        quoted = ", ".join(f"„{s}“" for s in missing)
        return (f" Заявката иска буквално {quoted}, а в записания код го няма: текстът, който "
                f"програмата показва или връща, е точно този — не превод (ОБЩО ≠ TOTAL).")

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
        words = self._missing_literals()
        if self._unrun:
            names = ", ".join(sorted(self._unrun))
            return (f"[проверка на кода] {names}: записан(и), но НЕ пуснат(и) след последната "
                    f"промяна. Преди да кажеш „готово“ — пусни кода (RUN_CMD) с примерите от "
                    f"заявката и с {_CASES}. {_MISMATCH} Ако кодът "
                    f"наистина не може да се пусне тук, кажи защо. {_ASSUMPTIONS}{words}")
        return ("[проверка на кода] Преди „готово“: примерите от заявката не доказват правилата. "
                f"Пусни кода с {_CASES}. {_MISMATCH} Ако вече си ги "
                f"пробвал — кажи в един ред кои и приключи. {_ASSUMPTIONS}{words}")
