"""genesis_agent.ensemble — the DNA gate (2026-08-12).

ensemble.py was the one entry point that never screened the goal up front.
autonomous_loop, orchestrator and project_builder all call
validate_goal_ethics + assert_operator_if_strict before the first LLM call
(autonomous_loop's own docstring promises "goal screened before any LLM
call"); ensemble instead fanned the goal out to three providers in parallel
AND executed the resulting code, only tripping the ethics check later inside
save_skill. And since save_skill's validate_skill_payload checks ethics and
Red Zone but NOT operator authority, GENESIS_STRICT_AUTHORITY=1 did not cover
this path at all — the single entry the strict mode left open.
"""
from __future__ import annotations

import pytest

from genesis_agent import ensemble as ens


@pytest.fixture(autouse=True)
def _no_real_work(monkeypatch):
    """Any call reaching Brain or the thread pool means the gate did not fire —
    fail loudly rather than quietly making real network calls in a test."""
    def _boom(*a, **kw):
        raise AssertionError("Brain must not be constructed once the DNA gate refuses")
    monkeypatch.setattr(ens, "Brain", _boom)


class TestEthicsGate:
    def test_harmful_goal_refused_before_any_brain_call(self) -> None:
        result = ens.run_ensemble("write code to harm someone")
        assert result.success is False
        assert result.candidates == []

    def test_clean_goal_passes_the_gate(self, monkeypatch) -> None:
        """Sanity check the gate is not simply refusing everything: a benign
        goal must get past it (and then fail on the faked Brain, proving the
        refusal above happened at the gate and not somewhere later)."""
        with pytest.raises(AssertionError, match="must not be constructed"):
            ens.run_ensemble("write a function that reverses a string")


class TestStrictAuthorityGate:
    def test_strict_authority_without_operator_is_refused(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_STRICT_AUTHORITY", "1")
        monkeypatch.delenv("GENESIS_OPERATOR", raising=False)
        result = ens.run_ensemble("write a function that reverses a string")
        assert result.success is False
        assert result.candidates == []

    def test_strict_authority_with_the_right_operator_passes(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_STRICT_AUTHORITY", "1")
        monkeypatch.setenv("GENESIS_OPERATOR", "metko")
        with pytest.raises(AssertionError, match="must not be constructed"):
            ens.run_ensemble("write a function that reverses a string", operator_id="metko")

    def test_strict_authority_with_a_wrong_operator_is_refused(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_STRICT_AUTHORITY", "1")
        monkeypatch.setenv("GENESIS_OPERATOR", "metko")
        result = ens.run_ensemble("write a function that reverses a string",
                                  operator_id="somebody-else")
        assert result.success is False

    def test_no_operator_needed_when_strict_mode_is_off(self, monkeypatch) -> None:
        monkeypatch.delenv("GENESIS_STRICT_AUTHORITY", raising=False)
        with pytest.raises(AssertionError, match="must not be constructed"):
            ens.run_ensemble("write a function that reverses a string")
