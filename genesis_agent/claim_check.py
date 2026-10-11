"""
genesis_agent.claim_check — съпоставя КАКВО твърди моделът с КАКВО реално е
изпълнил.

Най-скъпият начин един агент да се провали не е да откаже — а да каже
"готово, преместих снимките", когато нищо не се е преместило. Операторът
вярва и спира да проверява. Затова config.yaml's system_prompt казва изрично
"NEVER report an action as done unless a tool result above actually shows it
happening", а git историята пази два отделни бъга от същия клас ("Fix Genesis
handing work back instead of doing it", "Fix local model narrating tool use
without ever executing anything").

Дотук проверката беше груба: `looks_like_unverified_completion_claim()` пита
само дали в ТАЗИ реплика изобщо е текъл някакъв tool. Пропуска точно
интересния случай — моделът пуска един безобиден `LIST_DIR`, после обявява
"инсталирах пакета и преместих файловете". Има tool резултат, значи
проверката мълчи, а нито инсталация, нито преместване са се случвали.

Тук съпоставката е по ВИД: твърдение за инсталация иска реално изпълнена
инсталационна команда, твърдение за записан файл иска WRITE_FILE/EDIT_FILE
и т.н.

Нарочно консервативен: фалшивата тревога спира истинска работа и учи
оператора да я игнорира, а пропуснато твърдение го хваща следващата проверка
или самият оператор. Затова всяка категория иска ЯСЕН глагол в минало време
и се брои за неподкрепена само ако НИЩО подходящо не е текло.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Claim:
    """Едно открито твърдение и защо се води неподкрепено."""
    kind: str
    quote: str
    needed: str


_TEST_WORDS = frozenset({"pytest", "test", "tests", "unittest", "tox", "nose", "make",
                         "npm", "cargo", "go", "python", "python3"})
_PASS_KIND = "минаващи тестове"
_RAN_KIND = "пускане на тестове"

# Всяка категория: (какво твърди) → (кои инструменти биха го доказали,
# кои командни думи биха го доказали при RUN_CMD).
_CATEGORIES: tuple[tuple[str, re.Pattern[str], frozenset[str], frozenset[str]], ...] = (
    (
        "инсталация",
        re.compile(
            r"\b(инсталирах|инсталирано е|"
            r"i(?:'ve| have) installed|successfully installed)\b", re.IGNORECASE),
        frozenset({"RUN_CMD", "USE_SKILL", "DELEGATE"}),
        frozenset({"install", "apt", "apt-get", "pip", "pip3", "pipx", "npm", "yarn",
                   "pnpm", "cargo", "brew", "dnf", "pacman", "playwright", "uv"}),
    ),
    (
        "запис на файл",
        re.compile(
            r"\b(записах|създадох|запазих|"
            r"i(?:'ve| have) (?:written|created|saved)|"
            r"successfully (?:created|saved|written))\b", re.IGNORECASE),
        frozenset({"WRITE_FILE", "EDIT_FILE", "RUN_CMD", "USE_SKILL", "DELEGATE"}),
        frozenset({"tee", "cp", "mv", "touch", "mkdir", "curl", "wget", "git",
                   "python", "python3", "sh", "bash"}),
    ),
    (
        "преместване/триене",
        re.compile(
            r"\b(преместих|изтрих|преименувах|"
            r"i(?:'ve| have) (?:moved|deleted|removed|renamed)|"
            r"successfully (?:moved|deleted|removed|renamed))\b", re.IGNORECASE),
        frozenset({"RUN_CMD", "USE_SKILL", "DELEGATE"}),
        frozenset({"mv", "rm", "rmdir", "cp", "rename", "trash", "git"}),
    ),
    (
        _RAN_KIND,
        re.compile(
            # Само свършено (2026-10-07): „дали тестовете минават“ и „make sure
            # the tests pass“ са съвет към оператора, не твърдение.
            r"\b(пуснах тестовете|i(?:'ve| have) run the tests)\b", re.IGNORECASE),
        frozenset({"RUN_CMD", "USE_SKILL", "DELEGATE"}),
        _TEST_WORDS,
    ),
    (
        # Отделно от „пуснах“ (2026-10-11): „всички тестове минаха“ иска
        # ПОСЛЕДНОТО пускане на тестовете да е с rc=0. Измерено: pytest с rc=1
        # („1 failed, 1 passed“) минаваше като доказателство за „всички минаха“.
        # Разширено 2026-10-11 (e2e): след pytest с rc=1 минаваха „Всички 5
        # теста минават“, „Тестовете минават“, „All 5 tests pass“, „Всичките
        # тестове минаха успешно“ — и `genesis -p` излизаше с 0. Само формите с
        # „тестовете“/„всички“/„all“: броените („4 теста минават; test_vat
        # пада“, „3 от 5 теста минават“) са честни отчети — две ревюта
        # подред хванаха фалшива тревога там, затова извън шаблона.
        _PASS_KIND,
        re.compile(
            r"\b(тестовете (?:вече |сега |успешно )?(?:минаха|минават|преминават|преминаха)|"
            r"всички(?:те)? (?:\d+ )?(?:тестове|теста) (?:вече |сега |успешно )?"
            r"(?:минаха|минават|преминават|преминаха)|"
            r"tests? (?:now pass(?:es)?|passed)|"
            r"all (?:the )?(?:\d+ )?tests (?:now )?pass(?:ed)?)\b",
            re.IGNORECASE),
        frozenset({"RUN_CMD", "USE_SKILL", "DELEGATE"}),
        _TEST_WORDS,
    ),
    (
        # Измерено 2026-09-28: задача за уеб сайт, моделът само чете файловете
        # и пише „Файловете се отварят без грешки в браузър; анимациите, темата
        # и формата работят“. Браузър не е пускан; в страницата имаше счупен
        # og:image и нечетим текст върху градиента.
        "проверка в браузър",
        re.compile(
            r"(без грешки в (?:браузъра?|конзолата)|"
            r"(?:провер|теств)\w* (?:\w+ )?в браузъра?|"
            r"no (?:console )?errors in the browser|"
            r"i(?:'ve| have) (?:tested|checked|verified) (?:it |this |the \w+ )?in (?:the |a )?browser)",
            re.IGNORECASE),
        frozenset({"RUN_CMD", "BROWSE", "BROWSER_READ", "USE_SKILL", "DELEGATE"}),
        frozenset({"playwright", "chromium", "chrome", "msedge", "puppeteer", "selenium",
                   "lighthouse", "curl", "wget", "http.server", "node", "npx"}),
    ),
    (
        "конфигурация",
        re.compile(
            r"\b(конфигурирах|настроих|"
            r"i(?:'ve| have) configured|successfully configured)\b", re.IGNORECASE),
        frozenset({"WRITE_FILE", "EDIT_FILE", "RUN_CMD", "USE_SKILL", "DELEGATE"}),
        frozenset({"git", "systemctl", "chmod", "chown", "export", "sed", "tee",
                   "python", "python3", "sh", "bash", "npm", "pip"}),
    ),
)

# Достатъчно контекст, за да се вижда кое изречение е проблемното, без да се
# връща цялата реплика обратно на модела.
_QUOTE_RADIUS = 60


def _quote(text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - _QUOTE_RADIUS)
    end = min(len(text), match.end() + _QUOTE_RADIUS)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


# Изречението около „N теста минават“ (ревю 2026-10-11): по-широкият шаблон
# за _PASS_KIND четеше честния червен отчет като зелено твърдение — „Пуснах
# pytest: 4 теста минаха, 1 падна.“ след pytest rc=1 даваше подкана, „⚠
# червено“ и `genesis -p` с exit 1 (преди разширяването: exit 0, без
# предупреждения). Същото за „4 passed and 1 failed“, „Старите тестове
# минават, но новият пада“, „Провери дали 5 теста минават.“ и въпросите.
# Червена дума в същото изречение, въпрос или условие/отрицание преди
# съвпадението → не е твърдение за зелено. Нарочно консервативно (виж
# горе): пропуснато твърдение е по-евтино от фалшива тревога.
_SENTENCE_END_RE = re.compile(r"[.!?;](?=\s|$)|\n")
_RED_WORDS_RE = re.compile(
    r"\b(?:пада|падат|падна|паднаха|паднал[аио]?|падащ\w*|"
    r"не мина\w*|не минава\w*|червен\w*|освен|с изключение|"
    r"fail(?:s|ed|ing|ures?)?|except|\d+ errors?)\b", re.IGNORECASE)
_NOT_A_CLAIM_RE = re.compile(
    r"\b(?:дали|ако|когато|докато|провери|проверете|увери|"
    r"if|whether|unless|when|until|check|ensure|make sure)\b", re.IGNORECASE)
# Отрицание само точно пред съвпадението („Не всички тестове минават“): „не“
# другаде в изречението („Не беше лесно, но всички минаха“) не отменя твърдението.
_NEGATED_RE = re.compile(r"\b(?:не|not)\s+$", re.IGNORECASE)


def _honest_pass_report(text: str, match: re.Match[str]) -> bool:
    """Съвпадение за „тестовете минават“, което не твърди зелено: въпрос,
    условие, отрицание или смесен отчет с червена дума в същото изречение."""
    start = max((m.end() for m in _SENTENCE_END_RE.finditer(text, 0, match.start())),
                default=0)
    end_match = _SENTENCE_END_RE.search(text, match.end())
    end = end_match.end() if end_match else len(text)
    sentence = text[start:end]
    if sentence.rstrip().endswith("?") or re.match(r"\s+ли\b", text[match.end():]):
        return True
    before = text[start:match.start()]
    return bool(_RED_WORDS_RE.search(sentence) or _NOT_A_CLAIM_RE.search(before)
                or _NEGATED_RE.search(before))


def _find_claim(kind: str, pattern: re.Pattern[str], text: str) -> re.Match[str] | None:
    for match in pattern.finditer(text):
        if kind != _PASS_KIND or not _honest_pass_report(text, match):
            return match
    return None


def _supported(tool_names: set[str], command_text: str,
               ok_tools: frozenset[str], ok_words: frozenset[str]) -> bool:
    if not tool_names & ok_tools:
        return False
    # Инструмент като WRITE_FILE доказва сам себе си; RUN_CMD доказва нещо
    # само ако командата е от подходящия вид — иначе `RUN_CMD ls` би
    # "доказало" инсталация.
    if tool_names & (ok_tools - {"RUN_CMD"}):
        return True
    words = set(re.findall(r"[a-zA-Z0-9_.+-]+", command_text.lower()))
    return bool(words & ok_words)


# Резултат, носещ някой от тези, НЕ доказва извършено действие. Пропускането
# им обезсмисля проверката точно в случая, за който съществува: моделът иска
# инсталация, sandbox я блокира, моделът обявява "инсталирах пакета" — и
# твърдението минаваше, защото извикването е било записано. Записва се
# опитът, не резултатът.
_FAILURE_MARKERS = (
    "[SANDBOX BLOCKED]", "[SANDBOX DENIED]", "[SANDBOX DECLINED]",
    "Грешка при изпълнение", "Грешка при запис", "Грешка при четене",
    "не съществува", "Traceback (most recent call last)",
)

# Текстовият път връща резултати, започващи с името на тула: `[RUN_CMD: ls]`.
_RESULT_PREFIX_RE = re.compile(r"\[([A-Z_]+)[:\]]\s*([^\]]*)")


# Отказ на самия инструмент: `[WRITE_FILE: p] ❌ Файлът вече съществува…`,
# `[WRITE_FILE] Грешка: [Errno 13]…`, `[EDIT_FILE: p] ❌ Anchor-ът…`. Дотук се
# броеше за изпълнено и „записах app.py“ минаваше след отказан запис.
# И отказите на портата преди инструмента (e2e 2026-10-11): hook на оператора
# (`⛔ Спряно от hook`), режим план (`⏸`), спряно от оператора (`⏹`), пазачът
# на маркера и обхватът на под-агента (`⛔`). Измерено: WRITE_FILE, спрян от
# PreToolUse hook, и „Записах notes.md“ минаваше без подкана — файл нямаше.
_TOOL_REFUSED_RE = re.compile(r"^\[[A-Z_]+(?::[^\n]*?)?\]\s*(?:❌|⛔|⏸|⏹|Грешка)")

# Тестово пускане се познава по ГЛАВАТА на командата, не по думата „tests“
# където и да е (bug fix, 2026-10-11). Измерено: `git add tests/ && git commit`
# и `ls tests` (rc=0) след червен pytest се броеха за последно тестово пускане
# и „Всички тестове минаха“ минаваше; паднал `cat tests/fixtures/data.json`
# след зелен pytest оспорваше вярното твърдение.
# Разделители между команди във верига, в група — за да се види кой стои
# пред последната (_rc_is_the_runners). Самотният `&` (фон в bash, „после“ в
# cmd.exe) също дели: преглед 2026-10-11 — `cd app & npm test` не се броеше
# за тестово пускане. `2>&1`, `>&2`, `&>` са пренасочвания, не разделители.
_CHAIN_SEP_RE = re.compile(r"(&&|\|\||[;|&\n])")
_REDIRECT_RE = re.compile(r"\d*>&\d*|&>")


def _chain(command: str) -> list[str]:
    """Веригата през едно: [команда, разделител, команда, …]."""
    return _CHAIN_SEP_RE.split(_REDIRECT_RE.sub(" ", command or ""))



_TEST_TOOLS = frozenset({"pytest", "py.test", "tox", "nox", "nose", "nose2", "unittest", "jest",
                         "vitest", "mocha", "ctest", "phpunit", "rspec", "ava", "karma"})
_PY = frozenset({"python", "python3", "py"})
_PY_TEST_MODULES = frozenset({"pytest", "unittest", "nose2", "tox", "nox"})
# Подкоманда-цел: `npm test`, `npm run check`, `cargo test`, `make check`, `make test-unit`.
_TASK_TOOLS = frozenset({"npm", "yarn", "pnpm", "bun", "deno", "cargo", "go", "dotnet", "mvn",
                         "gradle", "gradlew", "make", "just", "rake", "mix", "composer"})
_TEST_TARGET_RE = re.compile(r"^(?:test|tests|check|spec)(?:[-:_.]\S*)?$")
# Име на скрипт с „test“ като отделна част: run_tests.sh, test_all.py — не latest.sh.
_TESTY_NAME_RE = re.compile(r"(?:^|[-_.])tests?(?:[-_.]|$)")
_WRAPPERS = frozenset({"sudo", "env", "uv", "poetry", "pipenv", "pdm", "hatch", "npx", "time",
                       "nice", "run", "exec", "timeout", "xvfb-run"})


def _head(token: str) -> str:
    base = re.split(r"[/\\]", token.strip("\"'"))[-1].lower()
    return base.removesuffix(".exe")


# Флагове, с които пускачът не пуска нито един тест. Одит 2026-10-11:
# `pytest --version` и `pytest --collect-only -q` (rc=0) след червен pytest се
# броеха за последно, зелено пускане и „Всички тестове минаха“ минаваше.
_NO_RUN_FLAGS = frozenset({"--version", "--help", "-h", "--collect-only", "--co"})
# `check` е тест-цел за make/npm/just (`make check`), но не и за cargo
# (`cargo check` само компилира) и Django (`python manage.py check` —
# системна проверка). Одит 2026-10-11: и двете с rc=0 скриваха червен тест.
_CHECK_IS_NOT_TESTS = frozenset({"cargo", "python"})


def _test_target(tool: str, target: str) -> bool:
    if not _TEST_TARGET_RE.match(target):
        return False
    return not (tool in _CHECK_IS_NOT_TESTS and target.startswith("check"))


def _segment_is_test_run(segment: str) -> bool:
    tokens = [t for t in segment.split() if t]
    # Обвивки и променливи отпред: `FOO=1 timeout 60 uv run pytest`.
    while tokens and ("=" in tokens[0] or _head(tokens[0]) in _WRAPPERS
                      or tokens[0].isdigit() or tokens[0].startswith("-")):
        tokens.pop(0)
    if not tokens or _NO_RUN_FLAGS & set(tokens[1:]):
        return False
    tool, rest = _head(tokens[0]), [_head(t) for t in tokens[1:]]
    if tool in _TEST_TOOLS:
        return True
    if re.fullmatch(r"python\d+(?:\.\d+)?", tool):
        tool = "python"
    if tool in _PY:
        args = [t for t in rest if not t.startswith("-") or t == "-m"]
        if args[:1] == ["-m"]:
            return len(args) > 1 and args[1] in _PY_TEST_MODULES
        # `python run_tests.py`, `python tests/test_x.py`, `python manage.py test`.
        return bool(args) and (bool(_TESTY_NAME_RE.search(args[0]))
                               or (len(args) > 1 and _test_target("python", args[1])))
    if tool in _TASK_TOOLS:
        targets = [t for t in rest if not t.startswith("-") and "=" not in t and t != "run"]
        if tool != "make":
            targets = targets[:1]
        return any(_test_target(tool, t) for t in targets)
    # Собствен скрипт: `./run_tests.sh`, `test.bat`. Самото `test` е
    # проверката на shell-а (`test -f x`), не тестове.
    return tool != "test" and bool(_TESTY_NAME_RE.search(tool))


def is_test_run(command: str) -> bool:
    """Пуска ли `command` тестове (поне един сегмент от веригата)."""
    return any(_segment_is_test_run(seg) for seg in _chain(command)[::2])


# Заглавието, което пише genesis_skills._tool_run_cmd: „[RUN_CMD: cmd]  (rc=N)“
# в края на ред. Одит 2026-10-11: rc се търсеше само на първия ред, така че
# при `cd app\npytest -q` (rc=1) се четеше rc=0; а `(rc=None)` (timeout,
# спряна или непусната команда) не пасваше на `-?\d+` и убит pytest минаваше
# за зелен. Командата се взема пак оттук: `[^\]]*` на текстовия път режеше
# `[ -d tests ] && pytest -q` до `[ -d tests `.
_HEADER_RE = re.compile(r"\[RUN_CMD: (.*?)\]  \(rc=(-?\d+|None)\)$", re.DOTALL | re.MULTILINE)
# Белег в края на аргументите на команда с rc≠0 — по него „всички тестове
# минаха“ разбира, че последното пускане е паднало, а останалите категории
# не броят командата.
_FAILED_RC_RE = re.compile(r" \(rc=(-?\d+)\)$")
# Червен изход на тестов пускач, независимо кой е: pytest/jest „1 failed“,
# unittest „FAILED (failures=1)“, go „FAIL“, cargo „test result: FAILED“.
# `FAIL` е с главни букви нарочно — „xfailed“, „XFAIL“ не са червени.
_RED_OUTPUT_RE = re.compile(
    r"\bFAIL(?:ED|URES)?\b|\b[1-9]\d* (?:failed|failing|failures?|errors?)\b")


def _rc_is_the_runners(command: str) -> bool:
    """rc на `command` ли е rc на тестовия пускач: пускачът е ПОСЛЕДНАТА
    команда на веригата и не стои след `||`. Така работи shell-ът: rc на
    верига е rc на последната пусната команда.

    Одит 2026-10-11: `pytest | tail`, `| tee`, `|| true`, `; echo`, `& echo`
    (cmd.exe) връщат rc на опашката — червен pytest доказваше „всички минаха“;
    а в `pytest && git commit` неуспешният commit правеше зеления pytest червен.
    Преглед 2026-10-11: първата поправка вярваше само на чиста `&&` верига и
    изхвърляше rc и при `cd app; pytest -q` — rc=4/5 (ImportError в conftest,
    „no tests ran“) и паднал `npm test` без „N failed“ минаваха за зелени.
    След `||` пускачът не тръгва, ако лявото е минало, и rc=0 не е негово;
    `pytest &` (фон) връща 0 веднага — и в двата случая решава изходът."""
    parts = _chain((command or "").rstrip().rstrip(";"))
    last, sep = parts[-1], (parts[-2] if len(parts) > 1 else "")
    return sep != "||" and _segment_is_test_run(last)


def counts_as_executed(name: str, args: str, result: str) -> tuple[str, str] | None:
    """Записът за `executed`, или None, ако резултатът не доказва изпълнение."""
    result = (result or "").lstrip()
    if _TOOL_REFUSED_RE.match(result):
        return None
    background = _RESULT_PREFIX_RE.match(result) if result.startswith("[RUN_BG:") else None
    if name.upper() == "RUN_CMD" and background:
        # Native RUN_CMD с background=true е фонова команда, не свършило
        # пускане (e2e 2026-10-11). Измерено: pytest rc=1, после същият pytest
        # във фона → „Всички тестове минават“ минаваше без подкана и без
        # „⚠ червено“, а текстовият таг [RUN_BG: …] се хващаше — записът е
        # същият като по текстовия път.
        name, args = "RUN_BG", background.group(2).strip()
    head = _HEADER_RE.match(result) if name.upper() == "RUN_CMD" else None
    if head is None:
        # Без заглавие с rc командата не е тръгнала (отказ на sandbox-а) или е
        # друг инструмент — тогава решават белезите за неуспех в текста.
        if any(marker in result for marker in _FAILURE_MARKERS):
            return None
        return (name, args)
    # Със заглавие командата е тръгнала и решава rc-то; изходът не го отменя.
    # Одит 2026-10-11: зелен pytest, който логва „Traceback“ или „не
    # съществува“, изпадаше — подкана, а след по-ранен червен — фалшиво „⚠
    # червено“ и `genesis -p` с exit 1.
    command = head.group(1)
    if head.group(2) == "None":
        rc = -1  # timeout или спряна: не е свършила
    elif is_test_run(command) and not _rc_is_the_runners(command):
        # rc е на опашката на веригата, не на пускача — решава изходът му.
        rc = 1 if _RED_OUTPUT_RE.search(result, head.end()) else 0
    else:
        rc = int(head.group(2))
    # Команда с rc≠0 е тръгнала, но не е свършила това, за което е пусната
    # (bug fix, 2026-10-11). Измерено: `pip install` с rc=1 доказваше
    # „Инсталирах пакета“, а `mv` с „cannot stat“ — „Преместих снимките“.
    # Записът остава, белязан с rc-то си: паднал тест пак е пуснат тест.
    return (name, f"{command} (rc={rc})") if rc else (name, command)


def _failed_rc(entry: tuple[str, str]) -> tuple[str, int]:
    """(командата без белега, rc) — rc е 0 за успешен запис."""
    args = entry[1] or ""
    mark = _FAILED_RC_RE.search(args)
    return (args[:mark.start()], int(mark.group(1))) if mark else (args, 0)


def _is_test_entry(entry: tuple[str, str]) -> bool:
    return entry[0].upper() == "RUN_CMD" and is_test_run(_failed_rc(entry)[0])


def _last_test_rc(executed: list[tuple[str, str]]) -> int:
    """rc на последното тестово пускане в `executed` (0 при липса на белег)."""
    for entry in reversed(executed):
        if _is_test_entry(entry):
            return _failed_rc(entry)[1]
    return 0


def _evidence(kind: str, executed: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Кои записи доказват `kind`. Паднала команда доказва нещо само ако е
    тестово пускане — „пуснах тестовете“, а за „всички минаха“ rc-то ѝ го
    опровергава в _last_test_rc. Одит 2026-10-11: паднали `npm run build`,
    `cargo build`, `npm install` доказваха „Пуснах тестовете, един пада“."""
    if kind in (_RAN_KIND, _PASS_KIND):
        return [e for e in executed if not _failed_rc(e)[1] or _is_test_entry(e)]
    return [e for e in executed if not _failed_rc(e)[1]]


def executed_from_text_results(results: list[str]) -> list[tuple[str, str]]:
    """Същото за текстовия tool път, където името стои в самия резултат.

    Живее тук, а не дублирано във всеки фронтенд: форматът, който се парсва,
    се произвежда от genesis_skills, и когато той се промени, двата цикъла
    иначе тихо губят доказателствата си едновременно.
    """
    out: list[tuple[str, str]] = []
    for result in results:
        match = _RESULT_PREFIX_RE.match(result or "")
        if not match:
            continue
        # За RUN_CMD с rc командата се взема от цялото заглавие (_HEADER_RE):
        # group(2) спира на първото „]“.
        entry = counts_as_executed(match.group(1), match.group(2), result)
        if entry:
            out.append(entry)
    return out


def unsupported_claims(text: str, executed: list[tuple[str, str]]) -> list[Claim]:
    """Твърденията в `text`, които изпълненото не подкрепя.

    `executed` е [(име_на_tool, аргументите като текст), ...] за ТОЗИ разговор
    — обикновено събрано от същия цикъл, който вече ги диспечира.

    Празен списък значи "нищо уличаващо", не "всичко е доказано": целта е да
    се хване очевидната симулация, не да се верифицира всяка дума.
    """
    if not text:
        return []
    found: list[Claim] = []
    for kind, pattern, ok_tools, ok_words in _CATEGORIES:
        match = _find_claim(kind, pattern, text)
        if not match:
            continue
        evidence = _evidence(kind, executed)
        tool_names = {name.upper() for name, _ in evidence}
        command_text = " ".join(arg for _, arg in evidence)
        if _supported(tool_names, command_text, ok_tools, ok_words):
            last_rc = _last_test_rc(executed) if kind == _PASS_KIND else 0
            if not last_rc:
                continue
            found.append(Claim(kind=kind, quote=_quote(text, match),
                               needed=f"тестово пускане с rc=0 (последното пускане на "
                                      f"тестовете е с rc={last_rc})"))
            continue
        needed = " / ".join(sorted(ok_tools - {"USE_SKILL", "DELEGATE"}))
        found.append(Claim(kind=kind, quote=_quote(text, match), needed=needed))
    return found


# Предупреждението, когато и след подканата отговорът твърди зелени тестове
# срещу червено последно пускане. `genesis -p` го брои за неуспех (2026-10-11:
# повторено „Всички тестове минават“ след pytest rc=1 даваше exit 0, ok:true).
RED_TESTS_WARNING = "⚠ Отговорът твърди, че тестовете минават, а последното им пускане е червено"


def green_claim_after_red(claims: list[Claim], executed: list[tuple[str, str]]) -> int:
    """rc на последното тестово пускане, ако сред `claims` има „тестовете
    минават“, а то е червено; иначе 0."""
    if any(c.kind == _PASS_KIND for c in claims):
        return _last_test_rc(executed)
    return 0


def nudge_text(claims: list[Claim]) -> str:
    """Бележката обратно към модела. Назовава КОНКРЕТНОТО твърдение — обща
    забележка ("не си го доказал") кара слаб модел да префразира вместо да
    изпълни."""
    lines = [("[Система]: Твърдиш неща, които нито един изпълнен инструмент "
              "по-горе не доказва:")]
    for c in claims:
        lines.append(f"  • {c.kind}: «{c.quote}» — очаквах {c.needed}, такова изпълнение няма.")
    lines.append("Ако действието е нужно — извикай инструмента СЕГА, не го описвай. "
                 "Ако вече е било изпълнено в по-ранен рунд, кажи го изрично и посочи кой "
                 "резултат го доказва.")
    return "\n".join(lines)


# Обещание в края на хода: моделът казва какво ЩЕ направи и спира. Измерено
# 2026-09-28 (задача за сайт, кодинг веригата — nemotron-ultra): „Ще използвам
# познатите координати… и ще продължа със създаването на сайта.“ — край на хода,
# нула файла. Само изрични глаголи за продължаване; изречение с „ако“/въпрос е
# предложение към оператора, не обещание.
_PROMISE = re.compile(
    r"(ще (?:продължа|започна|създам|напиша|направя|изградя)\b"
    r"|сега (?:ще|продължавам|започвам)\b|продължавам със?\b"
    r"|\b(?:i'll|i will|let me) (?:now )?(?:proceed|continue|start|create|write|build)\b)",
    re.IGNORECASE)
_OFFER = re.compile(r"\b(ако|if|желаете|искате|искаш)\b|\?", re.IGNORECASE)


def unfinished_promise(text: str) -> str:
    """Изречението, с което моделът обещава работа и спира; "" ако няма такова."""
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", text or "")[-4:]:
        if _PROMISE.search(sentence) and not _OFFER.search(sentence):
            return sentence.strip()
    return ""


def promise_nudge(sentence: str) -> str:
    return (f"[Система]: Написа «{sentence[:200]}», но ходът свърши без това да е направено. "
            "Не описвай следващата стъпка — извикай инструментите СЕГА и я направи.")
