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


def validate_goal_ethics(goal: str) -> None:
    # Whole words only, so "skill" does not trigger on "kill".
    if re.search(r'\bharm\b', goal.lower()) or re.search(r'\bkill\b', goal.lower()):
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
    if "HKEY_" in code and not red_zone_elevation_granted():
        raise GenesisDNAError("GENE-SECURITY: Red Zone access locked.")
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
