"""Глобалното състояние на sandbox-а не изтича от тест в тест (2026-10-09):
`rs.serve` в test_audit_2026_10_09 оставяше политика „пита телефона“ и по-късни
тестове получаваха „[SANDBOX DECLINED]“ според това какво се е пуснало преди тях.
Двата теста тук вървят един след друг (pytest пази реда във файла)."""
from __future__ import annotations

from genesis_agent import sandbox

_DEFAULT_MODE = sandbox._POLICY.mode


def test_1_a_test_that_leaves_global_state_behind() -> None:
    sandbox.set_policy(sandbox.SandboxPolicy(mode="interactive", confirm_fn=lambda *_: False))
    sandbox.before_exec = lambda: None


def test_2_the_next_test_starts_clean() -> None:
    assert sandbox._POLICY.mode == _DEFAULT_MODE
    assert sandbox.before_exec is None
