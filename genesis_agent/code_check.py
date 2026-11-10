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
from collections import Counter
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
# Одит 2026-10-22: и статичната проверка не е пускане — `python -m py_compile
# solution.py` (и ruff/mypy/black/compileall) махаше „НЕ пуснат“, а кодът не беше
# тръгвал (същото като непуснатия c229 в bench_fcc, 3/5). `-m pytest` остава пускане.
_NOT_A_RUN = re.compile(r"""["']?(?:\s+-\d+(?:\.\d+)?)?\s+(?:-m\s+(?:pip3?|py_compile|compileall|ruff"""
                        r"""|mypy|black|flake8|pyflakes|isort|pylint)\b|--version\b|-V(?:\s|$))""")
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
# bench sklad-package 2026-10-02: „ред ОБЩО“, а отчетът печата TOTAL 4/5 пъти (Claude —
# ОБЩО 2/2 със същата подкана). Само думата след ред/ключ/колона/надпис и текстът в
# кавички след →/печата/показва — не всяка главна дума (НАГОРЕ, ПАПКА, БУЛСТАТ).
_NAMED = re.compile(r"(?:\bред|\bключ|\bколона|\bнадпис|\bзаглавие)\s+[„\"«]?([А-ЯA-Z]{3,})\b")
_SHOWN = re.compile(r"(?:→|\bпечата|\bпоказва|\bизвежда|\bсъобщение)\s*[„\"«]([^“\"»\n]{2,60})[“\"»]")
# bench cli-config 2026-10-04: „още една колона продажна“ → sale_price. С малки букви само
# след „колона“ („командния ред сменя“ не е име) и без служебните думи след нея.
_COLUMN = re.compile(r"\bколона\s+[„\"«]?([а-яa-z][а-яa-z_]{2,})\b")
_NOT_NAMES = {"със", "във", "към", "без", "при", "след", "преди", "която", "като", "или"}


def literals(task: str) -> list[str]:
    """The strings the task wants shown verbatim, in their order."""
    found = sorted((m.start(1), m.group(1)) for rx in (_NAMED, _SHOWN, _COLUMN)
                    for m in rx.finditer(task or "") if m.group(1) not in _NOT_NAMES)
    return list(dict.fromkeys(s for _, s in found))


def _runs_python(command: str) -> bool:
    """Поне един сегмент на веригата пуска Python код (не pip, не --version)."""
    return any(not _NOT_A_RUN.match(seg, m.end())
               for seg in _SEGMENT_SPLIT.split(command)
               for m in _PYTHON_CMD.finditer(seg))


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
            # Думата трябва да е в програмата: тест с „ОБЩО“ не доказва, че отчетът го печата.
            if path.name.startswith("test_") or path.stem.endswith("_test") or "tests" in path.parts:
                continue
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


# ─── Разминаване в изхода на собствената проверка (2026-11-05) ───────────────
# bench_fcc c365 (30.09, „hard“ — три пускания, три провала): собственият скрипт
# печаташе „3 expected 4“ и накрая „All tests passed!“ — само print, rc=0.
# Моделът четеше последния ред и казваше „готово“; claim_check мълчеше — rc=0,
# а `python check.py` не е тестов пускач. Тук се чете самото разминаване:
# „expected X, got Y“ (и обратно, и „Y expected X“, и на два реда „Expected: X“ /
# „Got: Y“) с X ≠ Y, ред, който започва с ❌/✗/FAIL/MISMATCH/WRONG, „Failed: 2“,
# „4/5 passed“. Нарочно тясно: стойностите са числа, низове в кавички, списъци,
# True/False/None — „expected str, got int“ в съобщение за отхвърлен вход не е
# разминаване, нито ред с Error/invalid.
# Низ в кавички не започва веднага след буква (преглед 2026-11-05): в JSON/repr
# `{"expected": 3, "got": 3}, {"expected": 4, …` затварящата кавичка на ключа
# започваше „стойност“ `": 3, "` и еднаквите 3/3 излизаха разминаване.
# Хиляди с разделител (2026-11-06): „2,500,000“ / „1.000.000“ — цялото число, не
# „2,500“ и остатък, иначе „expected 2,500,000, got 2500001“ не се хващаше.
_GROUPED = r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d{1,3}(?:\.\d{3})+(?:,\d+)?"
_VALUE = (rf"""(?:{_GROUPED}|-?\d+(?:[.,]\d+)?|(?<!\w)'[^'\n]*'|(?<!\w)"[^"\n]*"|\[[^\]\n]*\]"""
          r"""|\([^)\n]*\)|\{[^}\n]*\}|<[^>\n]*>|True|False|None|true|false|null)""")
_EXP = r"(?:expected|expecting|очаквано|очакван[ао]?|очаквах|очаква)"
_GOT = (r"(?:but\s+(?:got|was|is|returned|received)|got|received|actual|returned|"
        r"получено|получен[ао]?|получих|върна(?:то)?)")
_SEP = r"\s*[:=]?\s*"
_EXP_FIRST = re.compile(rf"\b{_EXP}{_SEP}(?P<exp>{_VALUE})\s*[,;]?\s*{_GOT}{_SEP}(?P<got>{_VALUE})",
                        re.IGNORECASE)
_GOT_FIRST = re.compile(rf"\b{_GOT}{_SEP}(?P<got>{_VALUE})\s*[,;(]?\s*(?:but\s+)?{_EXP}{_SEP}"
                        rf"(?P<exp>{_VALUE})", re.IGNORECASE)
# „3 expected 4“, „count=3 (expected 4)“, „case 2: 3 expected 4“ — стойността е в
# началото на реда или след `=`/`:`, и само без дума за „получено“ на реда: в
# „Test 3 expected 4, got 4“ и „Test 1 expected 4“ числото е номерът на теста.
_BARE = re.compile(rf"(?:^\W*|[=:]\s*)(?P<got>{_VALUE})\s*[,;(]?\s*{_EXP}{_SEP}(?P<exp>{_VALUE})",
                   re.IGNORECASE)
_HAS_GOT = re.compile(rf"\b{_GOT}\b", re.IGNORECASE)
_LABEL_EXP = re.compile(rf"^\W*{_EXP}(?:\s+(?:output|result|value|резултат))?\s*[:=]\s*(?P<v>\S.*?)\s*$",
                        re.IGNORECASE)
_LABEL_GOT = re.compile(r"^\W*(?:got|actual|received|returned|output|получено|получих|върнато)"
                        r"(?:\s+(?:output|result|value|резултат))?\s*[:=]\s*(?P<v>\S.*?)\s*$",
                        re.IGNORECASE)
_FAIL_LINE = re.compile(r"^\s*(?:❌|✗|✘|(?-i:FAIL(?:ED)?)\b(?![:=]?\s*0\b)|(?-i:MISMATCH|WRONG)\b)")
# Преглед 2026-11-05, зелени итози: „OK (expected failures=1)“ на unittest
# (expectedFailure) не е паднал тест; в „passed=12 failed=0“ и „Passed: 12
# Failed: 0“ числото пред „failed“ е на passed — след „failed“ идва собственото му.
_FAIL_COUNT = re.compile(r"(?<!expected )\b(?:failed|failures?|wrong|грешни|паднали|неуспешни)\s*[:=]\s*[1-9]"
                         r"|\b[1-9]\d*\s+(?:failed|failing|wrong|грешни|паднаха|паднали)\b"
                         r"(?!\s*[:=]?\s*\d)",
                         re.IGNORECASE)
_RATIO = re.compile(r"\b(\d+)\s*/\s*(\d+)\s+(?:tests?\s+)?(?:passed|pass|ok|correct|верни|минаха|"
                    r"минават|успешни)\b", re.IGNORECASE)
# Ред на съобщение за грешка — там „expected 10, got 9“ е отказът, който проверката иска.
_ERROR_LINE = re.compile(r"\b(?:\w*error|\w*exception|invalid|невалид\w*|грешка|rejected|отхвърл\w*|"
                         r"raises?|raised|traceback)\b", re.IGNORECASE)
# Отговор, който сам казва, че нещо не е наред — честен отчет, не „готово“.
_ACKNOWLEDGED = re.compile(r"\b(?:пада|падат|падна|паднаха|не мина\w*|не минава\w*|червен\w*|"
                           r"разминава\w*|разминаване|грешн\w*|бъг\w*|нарочно|отхвърл\w*|"
                           r"fail(?:s|ed|ing|ures?)?|mismatch\w*|wrong|bug\w*|intentional\w*|"
                           r"reject\w*)\b|❌", re.IGNORECASE)
# Чете се само изходът на команда, която пуска код: `cat README.md` с „❌ не
# прави така“ или `grep FAIL logs/` не е собствена проверка.
# Одит 2026-11-10 (Windows и още няколко форми): `node.exe app.js`, `"C:\Program
# Files\nodejs\node.exe" app.js`, `.\app.exe`, `app.exe`, `run.bat`, `powershell
# -File .\run.ps1`, `uv run app.py`, `make run`, `tsx app.ts`, `Rscript a.R` не се
# брояха за пускане: „It works now.“ след зелено пускане беше „няма пускане“, а
# изходът им („expected 4, got 3“) не се четеше. Файл .exe/.bat/.cmd/.ps1 — само
# като командата (в началото или след ; & | ( ), не като аргумент на `cat`/`git add`.
_OTHER_CODE = re.compile(
    r"""(?:^|[\s;&|("'\\/])(?:(?:node|deno|bun|ruby|php|java|tsx|ts-node|rscript|pwsh|powershell)"""
    r"""(?:\.exe)?["']?|go\s+run|cargo\s+(?:run|test)|"""
    r"dotnet\s+(?:run|test)|npm\s+(?:test|run|start)|npx|yarn|pnpm|bash|sh|"
    r"uv\s+run|make\s+(?:run|test|check))(?=\s|$)"
    r"|(?:^|[\s;&|(])\.{1,2}[/\\]\S+(?=\s|$)"
    r"""|(?:^|[;&|(])\s*["']?[^\s;&|()"']*\.(?:exe|bat|cmd|ps1)["']?(?=\s|$)""",
    re.IGNORECASE)
_RUN_HEADER = re.compile(r"^\[RUN_CMD: (.*?)\]  \(rc=(-?\d+|None)\)$", re.DOTALL | re.MULTILINE)
_EDITED = re.compile(r"^\[(?:WRITE_FILE|EDIT_FILE): [^\]\n]+\] ✓", re.MULTILINE)


def runs_code(command: str) -> bool:
    """Пуска ли `command` код (Python, node, скрипт), а не само чете файлове."""
    return _runs_python(command) or bool(_OTHER_CODE.search(command))


_THOUSANDS = re.compile(r"-?\d{1,3}(?P<sep>[.,])\d{3}(?:(?P=sep)\d{3})*(?:(?!(?P=sep))[.,]\d+)?")


def _numbers(value: str) -> set[float]:
    """Какви числа може да е `value`. „1,000“ е и 1.0 (десетична запетая), и 1000
    (хиляди в изход на английски) — NEXT_STEPS 11-05: „Total: 1,000 expected 1000“
    излизаше разминаване, защото се четеше само като 1.0. Нарочно щедро: еднакво
    при някое от четенията = не е разминаване (фалшива тревога струва повече)."""
    out: set[float] = set()
    grouped = _THOUSANDS.fullmatch(value)
    if grouped:
        sep = grouped.group("sep")
        out.add(float(value.replace(sep, "").replace("," if sep == "." else ".", ".")))
    try:
        out.add(float(value.replace(",", ".")))
    except ValueError:
        pass
    return out


def _same_value(a: str, b: str) -> bool:
    a, b = a.strip().rstrip(".,;"), b.strip().rstrip(".,;")
    return a == b or bool(_numbers(a) & _numbers(b))


# Слабите сигнали (✗/FAIL ред, „N failed“, „4/5“, „3 expected 4“, Expected/Actual на
# два реда) — само в изход на проверка (преглед 2026-11-05: 11 от 28 нормални изхода
# на програми, чиято тема са неуспехите — „Imported 118 rows, 2 failed validation“,
# „Attempt 1 failed, retrying“, „✗ Обади се на мама“, „Score: 1/2 correct“, бюджет
# „Expected: 1200 лв / Actual: 1150 лв“ — даваха бележка). Проверка е: тестов
# пускач, скрипт с check/test/verify/valid/spec/assert в името, `python -c`/`node -e`,
# или изход, който сам казва „All tests passed“ (точно c365). Силното —
# „expected 4, got 3“ на един ред — важи за всеки пуснат код.
_CHECK_SCRIPT = re.compile(r"[\w./\\-]*(?:check|test|verify|valid|spec|assert)[\w.-]*\.(?:py|[cm]?js|ts|sh)\b"
                           r"|\b(?:python\d*(?:\.\d+)?|py|node)(?:\.exe)?\"?\s+-[ce]\b", re.IGNORECASE)
_SELF_SUMMARY = re.compile(r"\ball (?:\d+ )?(?:tests?|checks?|cases?) (?:passed|pass|ok)\b|\ball passed\b"
                           r"|\btests? passed\b|\bвсички (?:\d+ )?(?:тестове|теста|проверки|случаи) "
                           r"(?:минаха|минават)", re.IGNORECASE)


def is_self_check(command: str, output: str) -> bool:
    """Изходът е на проверка (тестове, check.py, `python -c`), не на програмата."""
    from genesis_agent.claim_check import is_test_run
    return is_test_run(command) or bool(_CHECK_SCRIPT.search(command) or _SELF_SUMMARY.search(output))


def mismatch_line(output: str, *, self_check: bool = True) -> str:
    """Първият ред от изхода, който показва разминаване; "" ако няма.
    Без `self_check` — само „expected X, got Y“ на един ред (виж горе)."""
    lines = (output or "").splitlines()
    for i, line in enumerate(lines):
        text = line.strip()
        if not text:
            continue
        if self_check and (_FAIL_LINE.match(text) or _FAIL_COUNT.search(text)):
            return text[:200]
        ratio = _RATIO.search(text) if self_check else None
        if ratio and int(ratio.group(1)) < int(ratio.group(2)):
            return text[:200]
        if _ERROR_LINE.search(text):
            continue
        pair = _EXP_FIRST.search(text) or _GOT_FIRST.search(text)
        if pair is None and self_check and not _HAS_GOT.search(text):
            pair = _BARE.search(text)
        if pair and not _same_value(pair.group("exp"), pair.group("got")):
            return text[:200]
        if not self_check:
            continue
        # „Expected: X“ и „Got: Y“ на съседни редове, в който и да е ред.
        exp = _LABEL_EXP.match(text)
        got = _LABEL_GOT.match(lines[i + 1].strip()) if exp and i + 1 < len(lines) else None
        if exp is None:
            got = _LABEL_GOT.match(text)
            exp = _LABEL_EXP.match(lines[i + 1].strip()) if got and i + 1 < len(lines) else None
        if exp and got and not _ERROR_LINE.search(lines[i + 1]) \
                and not _same_value(exp.group("v"), got.group("v")):
            return f"{text} / {lines[i + 1].strip()}"[:200]
    return ""


class MismatchCheck:
    """Като RunCheck: `observe` за всеки резултат, `due(отговор)` когато
    моделът спре да вика инструменти. Пази последното разминаване на всяка
    команда с rc=0, докато същата команда не мине чисто — или кодът не се
    смени и някое следващо пускане не мине чисто."""

    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, bool]] = {}   # команда → (ред, сменен ли е кодът след него)
        self._nudged = False

    def observe(self, result: str) -> None:
        result = result or ""
        if _EDITED.search(result):
            self._pending = {c: (line, True) for c, (line, _) in self._pending.items()}
            return
        head = _RUN_HEADER.match(result)
        if head is None or head.group(2) != "0":
            return   # не тръгна или е червена — това вече го вижда моделът и claim_check
        command = head.group(1).strip()
        if not runs_code(command):
            return
        output = result[head.end():]
        line = mismatch_line(output, self_check=is_self_check(command, output))
        if line:
            self._pending[command] = (line, False)
            return
        self._pending.pop(command, None)
        self._pending = {c: v for c, v in self._pending.items() if not v[1]}

    def due(self, reply: str) -> bool:
        if self._nudged or not self._pending:
            return False
        # Сменен код след разминаването и непуснат наново — винаги; иначе само
        # ако отговорът не казва сам, че нещо не е наред.
        return any(edited for _, edited in self._pending.values()) or not _ACKNOWLEDGED.search(reply or "")

    def note(self) -> str:
        self._nudged = True
        command, (line, edited) = next(reversed(self._pending.items()))
        rerun = (" След това разминаване кодът е сменен, а проверката не е пускана наново."
                 if edited else "")
        return (f"[проверка на изхода] `{command[:120]}` (rc=0) печата разминаване: «{line}».{rerun} "
                "rc=0 и „All tests passed!“ не го отменят — print не е assert. "
                f"{_MISMATCH} Ако разминаването е нарочно (напр. отказ на невалиден вход), "
                "кажи го в един ред и приключи.")


# ─── Сменена проверка в тест, който е бил преди хода (2026-11-05) ─────────────
# `genesis fix` не дава „✅ Поправено“ от отслабен тест от 22.10
# (repo_agent._weakened_tests); чатът — дава. Възпроизведено с фалшив модел и
# истински pytest: „оправи бъга, test_calc пада“ → EDIT_FILE на теста
# `assert add(2, 2) == 4` → `== 0`, pytest зелен, „Всички тестове минават.“ —
# без дума към оператора, бъгът в calc.py остава. Тест, написан в същия ход, е
# на модела и се поправя свободно (c125 в bench_fcc); тест отпреди хода е на
# оператора. Шаблоните са общи с repo_agent.
TEST_FILE_RE = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec)/"
    r"|(?:^|/)(?:test_[^/]*\.py|[^/]*_test\.(?:py|go)|conftest\.py)$"
    r"|\.(?:test|spec)\.[cm]?[jt]sx?$")
ASSERT_RE = re.compile(r"\bassert|\bexpect\s*\(|\.should\b|\bt\.(?:Error|Fatal)")
SKIP_RE = re.compile(
    r"\bmark\.(?:skip|skipif|xfail)\b|\bpytest\.(?:skip|xfail|importorskip)\s*\("
    r"|\bunittest\.(?:skip\w*|expectedFailure)\b|@(?:skip\w*|expectedFailure)\b"
    r"|\b(?:it|test|describe)\.(?:skip|todo)\b|\bx(?:it|test|describe)\s*\(|\bt\.Skip\w*\s*\(")
# Заявка, която сама иска тестът да се смени — тогава няма какво да се пита.
# „Оправи тестовете“ не е такава: обикновено значи „направи ги зелени“.
_TEST_CHANGE_ASKED = re.compile(
    r"\b(?:обнови|смени|промени|пренапиши|редактирай|коригирай|махни|изтрий|премахни|пропусни|"
    r"update|change|rewrite|edit|adjust|correct|remove|delete|skip|modify)\w*\s+(?:\S+\s+){0,3}?"
    r"(?:тест|test|assert|spec)"
    r"|\b(?:тест\w*|tests?|test_\w+)\s+(?:\S+\s+){0,2}?(?:греш(?:ен|н)|остар(?:ял|ел)|wrong|outdated|"
    r"incorrect|stale)"
    # Преглед 2026-11-05: „параметризирай тестовете“, „refactor the tests to use
    # parametrize“ — поисканото преработване маха старите assert редове.
    r"|\bпараметриз|\bparametri[sz]"
    r"|\b(?:рефакторирай|преработи|обедини|refactor)\w*\s+(?:\S+\s+){0,3}?(?:тест|test)",
    re.IGNORECASE)


def is_test_file(rel: str) -> bool:
    return bool(TEST_FILE_RE.search(rel.replace("\\", "/")))


def weakening(before: str, after: str) -> str:
    """Защо `after` е по-слаб тест от `before`: добавено пропускане или
    махнат/сменен ред с проверка; "" ако няма такова. Интервалите не се броят —
    `assert add(2,2)==4` → `assert add(2, 2) == 4` е форматиране, не промяна."""
    def lines(text: str) -> Counter[str]:
        return Counter(re.sub(r"\s+", "", ln) for ln in text.splitlines() if ln.strip())
    old, new = lines(before), lines(after)
    if any(SKIP_RE.search(ln) for ln in new - old):
        return "добавено пропускане (skip/xfail)"
    gone = next((ln for ln in before.splitlines()
                 if ASSERT_RE.search(ln) and (old - new)[re.sub(r"\s+", "", ln)]), "")
    return f"сменена или махната проверка `{gone.strip()[:120]}`" if gone else ""


class TestEditCheck:
    """Тестове отпреди хода, отслабени от WRITE/EDIT в него. `due(originals)`
    с `edit_history.turn_originals()`, когато моделът спре; веднъж на ход."""

    __test__ = False   # не е pytest клас, въпреки името

    def __init__(self, request: str = "", workspace: Path | None = None) -> None:
        self._asked = bool(_TEST_CHANGE_ASKED.search(request or ""))
        self._workspace = workspace
        self._found: list[str] = []
        self._nudged = False

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self._workspace).as_posix() if self._workspace else path.name
        except ValueError:
            return path.as_posix()

    def weakened(self, originals: dict[Path, bytes]) -> list[str]:
        found = []
        for path, old in originals.items():
            rel = self._rel(path)
            if not is_test_file(rel):
                continue
            try:
                new = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            why = weakening(old.decode("utf-8", errors="replace"), new)
            if why:
                found.append(f"{rel}: {why}")
        return found

    def due(self, originals: dict[Path, bytes]) -> bool:
        if self._nudged or self._asked:
            return False
        self._found = self.weakened(originals)
        return bool(self._found)

    def note(self) -> str:
        self._nudged = True
        listed = "; ".join(self._found[:5])
        return (f"[проверка на тестовете] Смени тест, който съществуваше преди хода: {listed}. "
                "Тестът е на оператора и описва искането: ако кодът не го изпълнява, поправя се "
                "кодът, не тестът. Тест се сменя само ако заявката го иска или тестът противоречи "
                "на условието — тогава го кажи изрично в отговора (кой ред и защо). Иначе върни "
                "теста както беше и поправи кода.")
