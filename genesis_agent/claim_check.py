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
        "пускане на тестове",
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
        # Разширено 2026-10-11 (e2e): след pytest с rc=1 минаваха най-честите
        # форми — „Всички 5 теста минават“, „Тестовете минават“, „All 5 tests
        # pass“, „Всичките тестове минаха успешно“, „… преминават успешно“ —
        # и `genesis -p` излизаше с 0. „дали/ако тестовете минават“ е въпрос,
        # не твърдение; „1 failed, 4 passed“ е цитиран изход, не зелено.
        _PASS_KIND,
        re.compile(
            r"(?<!дали )(?<!ако )(?<!дали всички )(?<!ако всички )"
            r"\b((?:всички(?:те)? )?(?:\d+ )?(?:тестовете|тестове|теста|тестът)"
            r" (?:вече |сега |успешно )?(?:минаха|минават|минава|мина|преминават|преминаха|преминава|премина)|"
            r"tests? (?:now pass(?:es)?|passed)|"
            r"all (?:the )?(?:\d+ )?tests (?:now )?pass(?:ed)?|"
            r"(?<!failed, )(?<!error, )(?<!errors, )\d+ passed(?!,\s*\d+ (?:failed|errors?)))\b",
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
_TEST_CMD_RE = re.compile(r"\b(pytest|unittest|tox|nose2?|test|tests|jest|vitest|mocha)\b", re.IGNORECASE)


_RC_RE = re.compile(r"\(rc=(-?\d+)\)")
# Белег в края на аргументите на тестово пускане с rc≠0 — по него „всички
# тестове минаха“ разбира, че последното пускане е паднало.
_FAILED_RC_RE = re.compile(r" \(rc=(-?\d+)\)$")


def counts_as_executed(name: str, args: str, result: str) -> tuple[str, str] | None:
    """Записът за `executed`, или None, ако резултатът не доказва изпълнение."""
    result = result or ""
    if _TOOL_REFUSED_RE.match(result.lstrip()):
        return None
    first = result.split("\n", 1)[0]
    rc_match = _RC_RE.search(first) if name == "RUN_CMD" else None
    rc = int(rc_match.group(1)) if rc_match else 0
    # Пуснат тест, който пада, пак е пуснат тест: `(rc=1)` и Traceback в
    # изхода значат, че командата е тръгнала. Без това вярното „Пуснах
    # тестовете: test_div пада“ се връщаше като неподкрепено. Падналото
    # пускане носи rc-то си — „всички тестове минаха“ не се доказва с него.
    if rc_match and _TEST_CMD_RE.search(args or ""):
        return (name, f"{args} (rc={rc})" if rc else args)
    # Всяка друга команда с rc≠0 не е свършила това, за което е пусната
    # (bug fix, 2026-10-11). Измерено: `pip install` с rc=1 доказваше
    # „Инсталирах пакета“, а `mv` с „cannot stat“ — „Преместих снимките“.
    if rc:
        return None
    if any(marker in result for marker in _FAILURE_MARKERS):
        return None
    return (name, args)


def _last_test_rc(executed: list[tuple[str, str]]) -> int:
    """rc на последното тестово пускане в `executed` (0 при липса на белег)."""
    for name, args in reversed(executed):
        if name.upper() == "RUN_CMD" and _TEST_CMD_RE.search(args or ""):
            mark = _FAILED_RC_RE.search(args)
            return int(mark.group(1)) if mark else 0
    return 0


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
    tool_names = {name.upper() for name, _ in executed}
    command_text = " ".join(arg for _, arg in executed)

    found: list[Claim] = []
    for kind, pattern, ok_tools, ok_words in _CATEGORIES:
        match = pattern.search(text)
        if not match:
            continue
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
