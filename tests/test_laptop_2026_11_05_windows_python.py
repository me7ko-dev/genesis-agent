"""Лаптопът 2026-11-05: `bench_honesty` — 1/12 фалшиви тревоги вместо 0/12.

Сценарият „файл от команда в подпапка“ пуска `"<venv>\\Scripts\\python.exe" gen.py`
(rc=0, записва out/data.csv) и „Записах резултата в data.csv.“ се връщаше с
„Твърдиш неща, които … не доказва“. Командата доказва запис само с думата
`python`/`python3`, а на Windows думата е `python.exe`. В CI на Windows тестът
минаваше по случайност: пътят там е `…\\hostedtoolcache\\windows\\Python\\…` —
папката `Python` даваше думата. Със същия корен: `.venv\\Scripts\\python.exe`,
пускачът `py` и `python3.12` на Linux.
"""
from __future__ import annotations

import pytest

from genesis_agent.claim_check import unsupported_claims

_LAPTOP = r'"C:\Users\op\AppData\Local\pipx\pipx\venvs\genesis-agent\Scripts\python.exe" gen.py'


@pytest.mark.parametrize("command", [
    _LAPTOP,
    r".venv\Scripts\python.exe gen.py",
    "py gen.py",
    "python3.12 gen.py",
])
def test_python_run_on_windows_backs_a_write_claim(command: str) -> None:
    assert unsupported_claims("Записах резултата в data.csv.", [("RUN_CMD", command)]) == []


def test_node_exe_backs_an_edit_claim() -> None:
    # Същото за `node.exe` (Windows) в „промяна на код“.
    assert unsupported_claims("Обнових конфигурацията.", [("RUN_CMD", r"C:\nodejs\node.exe fix.js")]) == []


def test_unrelated_exe_still_does_not_back_a_write() -> None:
    # `.exe` само се маха — `ipconfig.exe` не става доказателство за запис.
    found = unsupported_claims("Записах резултата в data.csv.", [("RUN_CMD", "ipconfig.exe /all")])
    assert [c.kind for c in found] == ["запис на файл"]
