"""genesis_agent.brain.Brain.escalate — who it is allowed to re-point.

`escalate()` upgrades the LOCAL fallback tier (3b→7b→14b). It used to set
`self.current` to that local model unconditionally, including while the
mission was running on a cloud model — found live on 2026-08-12 by tracing a
real mission, which printed "⬆️ Ескалация към по-голям модел:
qwen2.5-coder:7b-instruct-q3_K_M" one round after answering from
`ollama_cloud/gpt-oss:120b-cloud`.

Routing itself never followed that (`complete()` always walks the chain from
the top and overwrites `current` on success), but `brain.current` is read
BETWEEN two `complete()` calls by two callers that act on it —
`autonomous_loop._note_quality_failure` attributes the quality failure to
`current["provider"]`, and the critic passes `avoid=(provider, model)` from
`current` so it does not review its own code. Both get the wrong answer if
`current` names a model that never wrote anything.
"""
from __future__ import annotations

import pytest

from genesis_agent.brain import Brain


@pytest.fixture(autouse=True)
def _no_local_probe(monkeypatch):
    monkeypatch.setattr("genesis_agent.brain._local_available", lambda _m: False)


@pytest.fixture(autouse=True)
def _no_last_model(monkeypatch):
    monkeypatch.setattr("genesis_agent.brain._load_last_model", lambda: None)


@pytest.fixture
def _tiers(monkeypatch):
    """A deterministic 3b→7b local ladder, independent of what Ollama has."""
    monkeypatch.setattr("genesis_agent.model_router.next_tier_model",
                        lambda cur: "qwen2.5-coder:7b" if cur != "qwen2.5-coder:7b" else None)


def _brain_on_cloud() -> Brain:
    b = Brain()
    b.local = {"provider": "ollama_local", "model": "qwen2.5-coder:3b"}
    b.current = {"provider": "ollama_cloud", "model": "gpt-oss:120b-cloud"}
    return b


def _brain_on_local() -> Brain:
    b = Brain()
    b.local = {"provider": "ollama_local", "model": "qwen2.5-coder:3b"}
    b.current = b.local
    return b


class TestEscalateOnCloud:
    def test_current_is_not_hijacked_while_answering_from_the_cloud(self, _tiers) -> None:
        b = _brain_on_cloud()
        assert b.escalate() is True
        assert b.current == {"provider": "ollama_cloud", "model": "gpt-oss:120b-cloud"}

    def test_the_local_fallback_is_still_upgraded(self, _tiers) -> None:
        """The upgrade is the point — it just must not change this round's route."""
        b = _brain_on_cloud()
        b.escalate()
        assert b.local == {"provider": "ollama_local", "model": "qwen2.5-coder:7b"}

    def test_nothing_is_announced_to_the_operator(self, _tiers, capsys) -> None:
        b = _brain_on_cloud()
        b.escalate()
        assert "Ескалация" not in capsys.readouterr().out

    def test_the_quality_failure_is_still_charged_to_the_real_writer(self, _tiers) -> None:
        """The concrete consequence: autonomous_loop._note_quality_failure reads
        brain.current to decide which provider gets the black mark."""
        from genesis_agent import autonomous_loop

        b = _brain_on_cloud()
        b.escalate()
        recorded: list[str] = []
        import genesis_agent.provider_stats as ps
        orig = ps.record_call
        try:
            ps.record_call = lambda prov, lat, ok: recorded.append(prov)
            autonomous_loop._note_quality_failure(b, 0, threshold=99)
        finally:
            ps.record_call = orig
        assert recorded == ["ollama_cloud"]


class TestEscalateOnLocal:
    def test_current_follows_the_upgrade_when_actually_running_local(self, _tiers) -> None:
        b = _brain_on_local()
        assert b.escalate() is True
        assert b.current == {"provider": "ollama_local", "model": "qwen2.5-coder:7b"}

    def test_the_operator_is_told(self, _tiers, capsys) -> None:
        b = _brain_on_local()
        b.escalate()
        assert "Ескалация" in capsys.readouterr().out

    def test_no_bigger_tier_available_changes_nothing(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.model_router.next_tier_model", lambda cur: None)
        b = _brain_on_local()
        assert b.escalate() is False
        assert b.current == {"provider": "ollama_local", "model": "qwen2.5-coder:3b"}


class TestRouteForGoal:
    """Same rule, same reason: picking a local model for the goal must not
    claim the mission is running on it — the cloud chain is tried first."""

    def test_route_for_goal_does_not_hijack_a_cloud_current(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.model_router.pick_model",
                            lambda _goal: "qwen2.5-coder:14b")
        b = _brain_on_cloud()
        b.route_for_goal("build something")
        assert b.local["model"] == "qwen2.5-coder:14b"
        assert b.current == {"provider": "ollama_cloud", "model": "gpt-oss:120b-cloud"}

    def test_route_for_goal_still_re_points_a_local_only_brain(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.model_router.pick_model",
                            lambda _goal: "qwen2.5-coder:14b")
        b = _brain_on_local()
        b.route_for_goal("build something")
        assert b.current == {"provider": "ollama_local", "model": "qwen2.5-coder:14b"}
