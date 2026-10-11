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
        _PASS_KIND,
        re.compile(
            r"\b(тестовете (?:вече |сега )?(?:минаха|минават успешно)|"
            r"тестовете вече минават|всички тестове (?:минаха|минават)|"
            r"tests? (?:now pass(?:es)?|passed)|all (?:the )?tests pass)\b",
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
_TOOL_REFUSED_RE = re.compile(r"^\[[A-Z_]+(?::[^\n]*?)?\]\s*(?:❌|Грешка)")

# Тестово пускане се познава по ГЛАВАТА на командата, не по думата „tests“
# където и да е (bug fix, 2026-10-11). Измерено: `git add tests/ && git commit`
# и `ls tests` (rc=0) след червен pytest се броеха за последно тестово пускане
# и „Всички тестове минаха“ минаваше; паднал `cat tests/fixtures/data.json`
# след зелен pytest оспорваше вярното твърдение.
#
# Трета вълна (2026-10-11): чисто главовото разпознаване изпускаше обичайни
# форми, които старото търсене на дума хващаше — `python -W error -m pytest`,
# `timeout -s KILL 600 pytest`, `npm --prefix web test`, `bash -c 'pytest -q'`,
# `sh run_tests.sh`, `cargo nextest run`, `hatch run test`,
# `docker compose run app pytest` — и червено пускане в такава форма след
# зелен pytest оставяше „всички минаха“ да мине. Затова: познатите глави се
# разбират точно, известните НЕтестови глави (git, ls, cat, pip, линтери…)
# никога не са пускане на тестове, а за всяка друга глава важи старата широта —
# тестов инструмент или цел „test/check“ по-нататък в реда.
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")
_TEST_TOOLS = frozenset({"pytest", "py.test", "tox", "nox", "nose", "nose2", "unittest", "jest",
                         "vitest", "mocha", "ctest", "phpunit", "rspec", "ava", "karma"})
_PY = frozenset({"python", "python3", "py"})
_PY_RE = re.compile(r"python\d+(?:\.\d+)?")
_PY_TEST_MODULES = frozenset({"pytest", "unittest", "nose2", "tox", "nox"})
# Подкоманда-цел: `npm test`, `npm run check`, `cargo test`, `make check`, `make test-unit`.
_TASK_TOOLS = frozenset({"npm", "yarn", "pnpm", "bun", "deno", "cargo", "go", "dotnet", "mvn",
                         "mvnw", "gradle", "gradlew", "make", "just", "rake", "mix", "composer"})
_TEST_TARGET_RE = re.compile(r"^(?:test|tests|check|spec|nextest)(?:[-:_.]\S*)?$")
# Опции на _TASK_TOOLS, които взимат стойност: `npm --prefix web test`.
_VALUE_OPTS = frozenset({"--prefix", "--cwd", "--dir", "-c", "--manifest-path", "--filter",
                         "--workspace", "-w", "--project", "-f", "--file"})
# Име на скрипт с „test“ като отделна част: run_tests.sh, test_all.py — не latest.sh.
_TESTY_NAME_RE = re.compile(r"(?:^|[-_.])tests?(?:[-_.]|$)")
_WRAPPERS = frozenset({"sudo", "env", "uv", "poetry", "pipenv", "pdm", "hatch", "rye", "npx",
                       "pnpx", "bunx", "time", "nice", "ionice", "nohup", "stdbuf", "run", "exec",
                       "timeout", "xvfb-run", "command"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "cmd", "pwsh", "powershell"})
# Глави, които никога не пускат тестове, каквото и да стои след тях — четене,
# файлове, git, инсталация, линтери. Те НЕ скриват предишно червено пускане.
_NON_TEST_HEADS = frozenset({
    "git", "ls", "dir", "cat", "type", "head", "tail", "less", "more", "grep", "egrep", "rg",
    "ag", "find", "fd", "tree", "wc", "echo", "printf", "mkdir", "md", "touch", "cp", "copy",
    "mv", "move", "rm", "del", "erase", "rmdir", "rd", "ren", "rename", "ln", "chmod", "chown",
    "stat", "file", "du", "cd", "pushd", "popd", "sed", "awk", "sort", "uniq", "cut", "diff",
    "cmp", "tar", "zip", "unzip", "gzip", "curl", "wget", "pip", "pip3", "pipx", "apt",
    "apt-get", "brew", "dnf", "yum", "pacman", "choco", "winget", "code", "vim", "vi", "nano",
    "open", "start", "which", "where", "whereis", "realpath", "readlink", "basename", "dirname",
    "true", "false", "exit", "export", "set", "source", "tee", "jq", "xcopy", "robocopy",
    "attrib", "icacls", "ruff", "black", "flake8", "isort", "mypy", "pylint", "pyright",
    "eslint", "prettier", "tsc"})


def _head(token: str) -> str:
    base = re.split(r"[/\\]", token.strip("\"'"))[-1].lower()
    return base.removesuffix(".exe")


def _python_runs_tests(args: list[str]) -> bool:
    """Аргументите след интерпретатора (или след `coverage run`)."""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-W", "-X"):            # `-W error`, `-X dev` — флаг със стойност
            i += 2
            continue
        if arg == "-m":
            module = _head(args[i + 1]) if i + 1 < len(args) else ""
            if module == "coverage":
                return _coverage_runs_tests(args[i + 2:])
            return module in _PY_TEST_MODULES
        if arg == "-c":
            return bool(re.search(r"\b(?:pytest|unittest)\b", " ".join(args[i + 1:])))
        if arg.startswith("-"):
            i += 1
            continue
        # `python run_tests.py`, `python tests/test_x.py`, `python manage.py test`.
        return bool(_TESTY_NAME_RE.search(_head(arg))) or (
            i + 1 < len(args) and bool(_TEST_TARGET_RE.match(_head(args[i + 1]))))
    return False


def _coverage_runs_tests(args: list[str]) -> bool:
    """`coverage run -m pytest`, `coverage run tests.py`."""
    if not args or _head(args[0]) != "run":
        return False
    return _python_runs_tests(args[1:])


def _task_runs_tests(tool: str, args: list[str]) -> bool:
    if tool == "make":
        return any(_TEST_TARGET_RE.match(_head(t)) for t in args
                   if not t.startswith("-") and "=" not in t)
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-"):
            i += 2 if arg.lower() in _VALUE_OPTS else 1
            continue
        low = _head(arg)
        if low in ("run", "run-script", "exec", "x"):
            i += 1
            continue
        if low == "workspace":             # `yarn workspace web test`
            i += 2
            continue
        return bool(_TEST_TARGET_RE.match(low)) or low in _TEST_TOOLS
    return False


def _shell_runs_tests(args: list[str]) -> bool:
    for i, arg in enumerate(args):
        low = arg.lower()
        if low in ("-c", "/c", "/k", "-command", "-lc"):
            return is_test_run(" ".join(args[i + 1:]))
        if arg.startswith("-") or (arg.startswith("/") and len(arg) == 2):
            continue
        return bool(_TESTY_NAME_RE.search(_head(arg)))
    return False


def _tokens_run_tests(tokens: list[str]) -> bool:
    after_run = False
    # Обвивки, опции и променливи отпред: `FOO=1 timeout 60 uv run pytest`.
    while tokens and ("=" in tokens[0] or _head(tokens[0]) in _WRAPPERS
                      or tokens[0].isdigit() or tokens[0].startswith("-")):
        after_run = _head(tokens[0]) == "run"
        tokens = tokens[1:]
    if not tokens:
        return False
    tool, args = _head(tokens[0]), tokens[1:]
    if tool in _TEST_TOOLS:
        return True
    if after_run and _TEST_TARGET_RE.match(tool):
        return True                        # `hatch run test`, `poetry run test:unit`
    if tool == "test" or tool in _NON_TEST_HEADS:
        # Самото `test` е проверката на shell-а (`test -f x`), не тестове.
        return False
    if tool in _PY or _PY_RE.fullmatch(tool):
        return _python_runs_tests(args)
    if tool == "coverage":
        return _coverage_runs_tests(args)
    if tool in _TASK_TOOLS:
        return _task_runs_tests(tool, args)
    if tool in _SHELLS:
        return _shell_runs_tests(args)
    if tool == "node" and "--test" in args:
        return True
    # Собствен скрипт (`./run_tests.sh`, `test.bat`) или непозната глава —
    # обвивка с опции (`timeout -s KILL 600 pytest`, `env -u FOO pytest`),
    # контейнер (`docker compose run app pytest`), `./build.sh test`.
    if _TESTY_NAME_RE.search(tool):
        return True
    for j, arg in enumerate(args):
        low = _head(arg)
        if low in _NON_TEST_HEADS:
            return False                   # `sudo -u bob ls tests` — истинската глава е ls
        if low in _TEST_TOOLS or (not arg.startswith("-") and _TEST_TARGET_RE.match(low)):
            return True
        if low in _PY or low in _TASK_TOOLS or low in _SHELLS or low == "coverage" \
                or _PY_RE.fullmatch(low):
            return _tokens_run_tests(args[j:])
    return False


def is_test_run(command: str) -> bool:
    """Пуска ли `command` тестове (поне един сегмент от веригата)."""
    return any(_tokens_run_tests(seg.split())
               for seg in _SEGMENT_SPLIT_RE.split(command or ""))


# Заглавието на RUN_CMD: `[RUN_CMD: <команда>]  (rc=N)`. Търси се първото
# такова в целия резултат, не само в първия ред — многоредова команда
# (heredoc) иначе губеше rc-то си.
_RC_RE = re.compile(r"\]  \(rc=(-?\d+)\)$", re.MULTILINE)
_RUN_HEADER_RE = re.compile(r"\[RUN_CMD: (.*?)\]  \(rc=-?\d+\)$", re.MULTILINE | re.DOTALL)
# Белег в края на аргументите на тестово пускане с rc≠0 — по него „всички
# тестове минаха“ разбира, че последното пускане е паднало, а останалите
# категории не броят командата.
_FAILED_RC_RE = re.compile(r" \(rc=(-?\d+)\)$")


def command_from_result(result: str) -> str:
    """Командата от заглавието на RUN_CMD резултат ("" ако не е такъв)."""
    match = _RUN_HEADER_RE.match(result or "")
    return match.group(1) if match else ""


def counts_as_executed(name: str, args: str, result: str) -> tuple[str, str] | None:
    """Записът за `executed`, или None, ако резултатът не доказва изпълнение."""
    result = result or ""
    if _TOOL_REFUSED_RE.match(result.lstrip()):
        return None
    rc_match = _RC_RE.search(result) if name == "RUN_CMD" else None
    rc = int(rc_match.group(1)) if rc_match else 0
    # Тестово пускане с rc е тръгнало — зелено или червено. Изходът му не се
    # гледа за _FAILURE_MARKERS (bug fix, 2026-10-11, трета вълна): unittest
    # не прихваща stderr и тест, който упражнява logging.exception, печата
    # „Traceback (most recent call last)“ и при „OK“. Измерено: зелено
    # пускане с такъв изход изпадаше и вярното „Пуснах тестовете — всички
    # тестове минаха“ се връщаше като неподкрепено. Червеното носи rc-то си —
    # „всички тестове минаха“ не се доказва с него (_last_test_rc).
    if rc_match and is_test_run(args):
        return (name, f"{args} (rc={rc})" if rc else args)
    # Всяка друга команда с rc≠0 не е свършила това, за което е пусната, и не
    # доказва нищо — нито „Инсталирах пакета“, нито „Пуснах тестовете“
    # (bug fix, 2026-10-11). Измерено: `pip install` с rc=1 доказваше
    # инсталацията, `mv` с „cannot stat“ — преместването, а паднал build или
    # `npm install` — „Пуснах тестовете“ (думите „python“/„npm“ в командата).
    if rc:
        return None
    if any(marker in result for marker in _FAILURE_MARKERS):
        return None
    return (name, args)


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
    """Кои записи доказват `kind`. Белязан (паднал) запис доказва само
    тестовите категории и само ако е тестово пускане: „пуснах тестовете“ е
    вярно и при червено, а за „всички минаха“ влиза, за да го опровергае в
    _last_test_rc."""
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
        match = pattern.search(text)
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
