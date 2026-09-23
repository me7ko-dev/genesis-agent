"""
genesis_agent.dna — the agent's non-negotiable operating principles.

These are injected into every system prompt. They are deliberately short: a
principle the model can hold in working memory beats a policy document it
skims. Anything that must NOT depend on the model's goodwill lives in
`sandbox.py` as a real mechanism, not here.

The operator identity is configurable, not hardcoded: set
`GENESIS_OPERATOR` to your own name/handle. It is only consulted when
`GENESIS_STRICT_AUTHORITY=1` — off by default, so a fresh install does not
need it at all.
"""

from __future__ import annotations

import os
import re
from typing import Final


# --- THE PRINCIPLES (immutable constants) ---
class DNA:
    SOVEREIGNTY: Final = "GENE-SOVEREIGNTY: You are an autonomous unit, accountable to your operator."
    ACCURACY: Final = "GENE-PRECISION: No placeholders. Dynamic resolution only."
    LOYALTY: Final = "GENE-LOYALTY: Serve the operator who runs you; never act against their interest."
    ETHICS: Final = "GENE-ETHICS: Never facilitate harm to humans."
    SECURITY: Final = "GENE-SECURITY: Red Zone access is forbidden without tokens."
    QUALITY: Final = "GENE-QUALITY: Senior engineer standards (PEP8, type hints)."
    AUTONOMY: Final = "GENE-AUTONOMY: Self-correction and recursive improvement."
    POLYGLOT: Final = "GENE-POLYGLOT: Mastery of all major programming languages."
    ALCHEMY: Final = "GENE-ALCHEMY: Secure data transformation and synthesis."


def sovereign_operators() -> tuple[str, ...]:
    """
    Whoever this install belongs to. Configured, never hardcoded — a public
    agent has no business shipping with someone else's name baked in.

    `GENESIS_OPERATOR` accepts one name or several, comma-separated.
    Empty (the default) means the concept is simply not in use.
    """
    raw = os.environ.get("GENESIS_OPERATOR", "").strip()
    if not raw:
        return ()
    return tuple(p.strip().upper() for p in raw.split(",") if p.strip())


class GenesisDNAError(Exception):
    """Base exception for DNA violations."""


def dna_system_prompt_addon() -> str:
    return (
        "\n\n## CORE PRINCIPLES (NON-NEGOTIABLE)\n"
        f"1. {DNA.ETHICS}\n"
        f"2. {DNA.LOYALTY}\n"
        f"3. {DNA.SECURITY}\n"
        f"4. {DNA.ACCURACY}\n"
        f"5. {DNA.QUALITY}\n"
        f"6. {DNA.POLYGLOT}\n"
        f"7. {DNA.ALCHEMY}\n"
        f"8. {DNA.AUTONOMY}\n"
        "9. GENE-IDENTITY: You are Genesis Agent, a self-hosted autonomous coding agent.\n"
    )


# --- SECURITY & ETHICS ---
def red_zone_elevation_granted() -> bool:
    token = os.environ.get("GENESIS_RED_ZONE_TOKEN")
    secret = os.environ.get("GENESIS_RED_ZONE_SECRET")
    # Both unset would otherwise compare None == None and silently elevate.
    return bool(token) and token == secret


# Кого се опитва да нарани и с какво. Двете трябва да са ЗАЕДНО, в едно
# изречение: само по глагола не става разлика между "kill a stuck process" и
# "kill people", а първото е ежедневна работа за кодинг агент.
_HUMAN_TARGET = (
    r"(?:people|persons?|humans?|someone|somebody|anyone|civilians?|"
    r"children|child|kids?|victims?|famil(?:y|ies)|neighbou?rs?|"
    r"хора(?:та)?|човек(?:а|ът)?|деца|дете|някого|някой|семейств\w*|съсед\w*)"
)
_HARM_ACT = (
    r"(?:harm|hurt|injure|maim|kill|kills|killing|murder|assassinat\w*|poison|"
    r"навред\w*|нараня\w*|уби(?:й|ва|ване|ец|я|ем)?|избий|отрови)"
)
# Техническият смисъл на "kill" има свой обект. Изречение като "kill the
# process that people started" съдържа и глагола, и думата "people" на 17
# знака разстояние — затова техническите двойки се махат ПРЕДИ търсенето,
# вместо да се залага на разстоянието.
_TECHNICAL_KILL_RE = re.compile(
    r"\b(?:kill|kills|killing|уби(?:й|ва|ване)?)\b"
    r"(?:\s+-\w+)?"                                    # kill -9, kill -SIGTERM
    r"\s+(?:the|a|an|this|that|my|all|any)?\s*"
    r"(?:\w+\s+){0,2}"                                 # "stuck", "runaway python"
    r"(?:process(?:es)?|container\w*|jobs?|tasks?|threads?|servers?|daemons?|"
    r"services?|sessions?|connections?|quer(?:y|ies)|ports?|pids?|apps?|scripts?|"
    r"nodes?|workers?|timers?|pods?|vms?|instances?|shells?|tabs?|"
    r"процес\w*|задач\w*|нишк\w*|сървър\w*|услуг\w*|скрипт\w*)\b",
    re.IGNORECASE,
)

# До 30 знака между действието и жертвата и без прескачане на изречение.
_HARM_RE = re.compile(
    rf"\b{_HARM_ACT}\b[^.\n]{{0,30}}?\b{_HUMAN_TARGET}\b"
    rf"|\b{_HUMAN_TARGET}\b[^.\n]{{0,30}}?\b{_HARM_ACT}\b",
    re.IGNORECASE,
)


def validate_goal_ethics(goal: str) -> None:
    """Спъващ кабел за явно заявено намерение за насилие над хора.

    НЕ е защитен механизъм и не се представя за такъв — всяко пренаписване го
    заобикаля. Истинските механизми са в `sandbox.py` (реални граници при
    изпълнение) и в отказа на самия модел. Смисълът тук е една проверка ПРЕДИ
    първото обръщение към модел, на всички входове (autonomous_loop, ensemble,
    orchestrator, project_builder, save_skill).

    Измерено преди пренаписването, старото правило (`\bharm\b` или `\bkill\b`
    някъде в целта) се държеше точно обратно на предназначението си:

        ОТКАЗВАШЕ  "kill a stuck process", "kill -9 a runaway container",
                   "harm reduction report parser"
        ПУСКАШЕ    "write a tool that kills people"  (kills != kill)
                   "убий хората"                     (не е на английски)

    Тоест спираше обикновена системна работа на всички входове, а изречението,
    заради което съществува, минаваше. Сега действието и жертвата се търсят
    ЗАЕДНО — и на български, защото операторът пише на български.
    """
    # Техническото "убий процеса" се маха първо; каквото остане, се проверява.
    # "kill the process, then kill people" пак се хваща — втората клауза остава.
    cleaned = _TECHNICAL_KILL_RE.sub(" ", goal or "")
    if _HARM_RE.search(cleaned):
        raise GenesisDNAError("GENE-ETHICS: Goal violates the humanity shield.")


def validate_skill_payload(goal: str, code: str) -> None:
    validate_goal_ethics(goal)
    if "HKEY_" in code and not red_zone_elevation_granted():
        raise GenesisDNAError("GENE-SECURITY: Red Zone access locked.")


def is_sovereign_operator(name: str | None) -> bool:
    if not name:
        return False
    masters = sovereign_operators()
    if not masters:
        return False
    return name.strip().upper() in masters


def validate_code_before_execution(code: str) -> str | None:
    """Returns a reason string when blocked, None when clear — never raises.

    Its two callers (executor.run_python_subprocess/_inprocess) convert a
    truthy return into a graceful ExecResult(ok=False, ...); this used to
    raise GenesisDNAError instead, which the return type never promised and
    which neither caller caught — an uncaught exception straight out of a
    mission's code-execution step instead of the intended failure result
    (bug found writing executor tests, 2026-09-18)."""
    if "HKEY_" in code and not red_zone_elevation_granted():
        return "GENE-SECURITY: Red Zone access locked."
    return None


def assert_operator_if_strict(operator_id: str | None) -> None:
    """
    Opt-in. With `GENESIS_STRICT_AUTHORITY=1` and no `GENESIS_OPERATOR` set,
    this refuses everything rather than letting anyone through — a misconfigured
    authority gate should fail closed.
    """
    if os.environ.get("GENESIS_STRICT_AUTHORITY") == "1" and not is_sovereign_operator(operator_id):
        raise GenesisDNAError("GENE-AUTHORITY: Unauthorized operator.")


def format_operator_audit(operator_id: str | None) -> dict[str, object]:
    return {
        "operator": operator_id,
        "is_sovereign": is_sovereign_operator(operator_id),
        "red_zone_active": red_zone_elevation_granted(),
    }
