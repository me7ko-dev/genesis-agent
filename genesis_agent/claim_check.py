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
            r"\b(пуснах тестовете|тестовете мина\w*|"
            r"i(?:'ve| have) run the tests|tests? (?:now )?pass(?:ed|es)?)\b",
            re.IGNORECASE),
        frozenset({"RUN_CMD", "USE_SKILL", "DELEGATE"}),
        frozenset({"pytest", "test", "tests", "unittest", "tox", "nose", "make",
                   "npm", "cargo", "go", "python", "python3"}),
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
