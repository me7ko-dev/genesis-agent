"""genesis_agent.model_router — adaptive local-model tier selection, called
directly from brain.py's local-model path (`_call_local`'s tier pick and its
failure-escalation). Zero coverage before this file despite being core
routing logic, not a peripheral feature.
"""
from __future__ import annotations

import genesis_agent.model_router as mr


class _FakeResponse:
    def __init__(self, status_code: int, models: list[str] | None = None) -> None:
        self.status_code = status_code
        self._models = models or []

    def json(self):
        return {"models": [{"name": n} for n in self._models]}


class TestEstimateTier:
    def test_empty_or_none_goal_is_tier_0(self) -> None:
        assert mr.estimate_tier("") == 0
        assert mr.estimate_tier(None) == 0

    def test_plain_goal_with_no_keywords_is_tier_0(self) -> None:
        assert mr.estimate_tier("say hello") == 0

    def test_medium_keyword_is_tier_1(self) -> None:
        assert mr.estimate_tier("implement bubble sort of a list") == 1

    def test_hard_keyword_is_tier_2(self) -> None:
        assert mr.estimate_tier("build a recursive descent parser") == 2

    def test_hard_keyword_beats_medium_keyword_present_in_same_goal(self) -> None:
        assert mr.estimate_tier("sort a graph's nodes") == 2  # "sort" (med) + "graph" (hard)

    def test_long_goal_bumps_tier_when_below_max(self) -> None:
        short = "say hello"
        # > 220 chars, no keywords, no "and"/commas (those would double-bump).
        long_goal = "please write some code for this task " * 8
        assert len(long_goal) > 220
        assert mr.estimate_tier(short) == 0
        assert mr.estimate_tier(long_goal) == 1

    def test_many_clauses_bump_tier_when_below_max(self) -> None:
        goal = "do a, do b, do c, do d, do e"  # 4+ commas, no keywords
        assert mr.estimate_tier(goal) == 1

    def test_score_never_exceeds_2_even_with_every_bump(self) -> None:
        goal = "build a recursive descent parser, " * 10  # hard keyword + long + many commas
        assert mr.estimate_tier(goal) == 2


class TestAvailableTiers:
    def test_all_false_on_connection_error(self, monkeypatch) -> None:
        def _raise(*a, **kw):
            raise mr.requests.RequestException("no ollama running")
        monkeypatch.setattr(mr.requests, "get", _raise)
        assert mr.available_tiers() == [False, False, False]

    def test_all_false_on_non_200(self, monkeypatch) -> None:
        monkeypatch.setattr(mr.requests, "get", lambda *a, **kw: _FakeResponse(500))
        assert mr.available_tiers() == [False, False, False]

    def test_detects_installed_tiers_by_name(self, monkeypatch) -> None:
        monkeypatch.setattr(mr, "LOCAL_TIERS", ["qwen2.5-coder:3b", "qwen2.5-coder:7b", "qwen2.5-coder:14b"])
        monkeypatch.setattr(mr.requests, "get",
                            lambda *a, **kw: _FakeResponse(200, ["qwen2.5-coder:3b", "llama3:8b"]))
        assert mr.available_tiers() == [True, False, False]


class TestPickModel:
    def test_returns_none_when_nothing_installed(self, monkeypatch) -> None:
        monkeypatch.setattr(mr, "available_tiers", lambda: [False, False, False])
        assert mr.pick_model("build a parser") is None

    def test_returns_exact_tier_when_available(self, monkeypatch) -> None:
        monkeypatch.setattr(mr, "available_tiers", lambda: [True, True, True])
        assert mr.pick_model("build a parser") == mr.LOCAL_TIERS[2]  # hard -> tier 2

    def test_falls_down_to_a_smaller_available_tier(self, monkeypatch) -> None:
        """Wants tier 2 (hard goal) but only tier 0 is installed."""
        monkeypatch.setattr(mr, "available_tiers", lambda: [True, False, False])
        assert mr.pick_model("build a parser") == mr.LOCAL_TIERS[0]

    def test_falls_up_when_nothing_smaller_is_available(self, monkeypatch) -> None:
        """Easy goal (tier 0) but only tier 2 is installed."""
        monkeypatch.setattr(mr, "available_tiers", lambda: [False, False, True])
        assert mr.pick_model("say hello") == mr.LOCAL_TIERS[2]


class TestNextTierModel:
    def test_escalates_to_the_next_available_tier(self, monkeypatch) -> None:
        monkeypatch.setattr(mr, "available_tiers", lambda: [True, True, True])
        assert mr.next_tier_model(mr.LOCAL_TIERS[0]) == mr.LOCAL_TIERS[1]

    def test_skips_unavailable_tiers_when_escalating(self, monkeypatch) -> None:
        monkeypatch.setattr(mr, "available_tiers", lambda: [True, False, True])
        assert mr.next_tier_model(mr.LOCAL_TIERS[0]) == mr.LOCAL_TIERS[2]

    def test_none_when_already_at_the_top_tier(self, monkeypatch) -> None:
        monkeypatch.setattr(mr, "available_tiers", lambda: [True, True, True])
        assert mr.next_tier_model(mr.LOCAL_TIERS[2]) is None

    def test_unknown_current_model_starts_escalation_from_the_bottom(self, monkeypatch) -> None:
        monkeypatch.setattr(mr, "available_tiers", lambda: [True, False, False])
        assert mr.next_tier_model("some-model-not-in-the-list") == mr.LOCAL_TIERS[0]

    def test_none_current_starts_escalation_from_the_bottom(self, monkeypatch) -> None:
        monkeypatch.setattr(mr, "available_tiers", lambda: [False, True, False])
        assert mr.next_tier_model(None) == mr.LOCAL_TIERS[1]
