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
_PYTHON_CMD = re.compile(r"""(?:^|[\s"'/\\&|;(])(?:python(?:\d+(?:\.\d+)?)?|py|pytest)(?:\.exe)?(?=[\s"']|$)""",
                         re.IGNORECASE | re.MULTILINE)
# Python команда, която не пуска кода: инсталация или проверка на версията.
# Гледа се САМО веднага след интерпретатора и във всеки сегмент на веригата
# поотделно (bug fix, 2026-10-11). Измерено: търсено в целия ред,
# `pip install -r requirements.txt && python solution.py` и
# `python solution.py --version` оставяха „записан, но НЕ пуснат“.
_NOT_A_RUN = re.compile(r"""["']?(?:\s+-\d+(?:\.\d+)?)?\s+(?:-m\s+pip3?\b|--version\b|-V(?:\s|$))""")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n]")
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


def _runs_python(command: str) -> bool:
    """Поне един сегмент на веригата пуска Python код (не pip, не --version)."""
    return any(not _NOT_A_RUN.match(seg, m.end())
               for seg in _SEGMENT_SPLIT.split(command)
               for m in _PYTHON_CMD.finditer(seg))


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
        # Пуснат = командата е тръгнала (заглавието завършва с „(rc=“;
        # отказаната от sandbox-а няма rc) и наистина пуска код (bug fix,
        # 2026-10-11). Измерено: `python -m pip install requests` и
        # `[SANDBOX DECLINED]` за `python solution.py` махаха „записан, но НЕ
        # пуснат“ и моделът получаваше само по-меката бележка.
        head = result.split("]  (rc=", 1)
        command = head[0].removeprefix("[RUN_CMD: ")
        started = len(head) == 2 and "\n[SANDBOX" not in head[0]
        if (self._unrun and _RUN.match(result) and started
                and _runs_python(command)):
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
                    f"заявката и с {_CASES}. {_MISMATCH} Ако кодът "
                    f"наистина не може да се пусне тук, кажи защо. {_ASSUMPTIONS}")
        return ("[проверка на кода] Преди „готово“: примерите от заявката не доказват правилата. "
                f"Пусни кода с {_CASES}. {_MISMATCH} Ако вече си ги "
                f"пробвал — кажи в един ред кои и приключи. {_ASSUMPTIONS}")
