"""genesis_agent.model_router — adaptive local-model tier selection, called
directly from brain.py's local-model path (`_call_local`'s tier pick and its
failure-escalation). Zero coverage before this file despite being core
routing logic, not a peripheral feature.
"""
from __future__ import annotations

import pytest

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
    @pytest.fixture(autouse=True)
    def _fixed_tiers(self, monkeypatch) -> None:
        # LOCAL_TIERS is read from GENESIS_TIER0/1/2 env vars at import time.
        # A dev machine with those exported can end up with duplicate
        # entries in the real list, which breaks the .index()-based lookup
        # these tests assume is unambiguous. Pin a known, distinct set
        # instead of trusting whatever the environment happens to hold.
        monkeypatch.setattr(mr, "LOCAL_TIERS", ["tier-a", "tier-b", "tier-c"])

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


# ── Чат маршрутизация: лек въпрос → малък модел ──────────────────────────────
# Примерите са от реалния начин, по който операторът пише — кирилица И латиница.

@pytest.mark.parametrize("text", [
    "здрасти",
    "какво е рекурсия?",
    "колко е 17 по 23",
    "obqsni mi razlikata mejdu tcp i udp",
    "what is a closure in python?",
    "благодаря!",
])
def test_plain_questions_are_light(text) -> None:
    assert mr.is_light_request(text) is True


@pytest.mark.parametrize("text", [
    "nameri i napravi backup genesis v disk D: v zip fail",   # реално съобщение
    "napravi go v C diska",
    "поправи бъга в stats.py",
    "да",                      # потвърждение продължава предишната задача
    "davai",
    "da, slei PR-a i pochistvai",
    "колко файла има на десктопа?",
    "fix the failing test",
    "какво има в C:\\Users\\roika",
    "виж https://example.com",
    "```print(1)```",
    "x" * 200,                 # дълго = вероятно задача
    "какво време е навън?",    # актуално → търсене в мрежата
    "kakav e kursa na evroto",
    "latest news about AI",
])
def test_work_and_confirmations_go_to_the_strong_model(text) -> None:
    assert mr.is_light_request(text) is False


def test_mid_tool_loop_is_never_light() -> None:
    msgs = [{"role": "user", "content": "здрасти"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "..."}]
    assert mr.is_light_turn(msgs) is False


def test_a_follow_up_right_after_tool_work_is_not_light() -> None:
    """„а колко са?" след търсене на файлове е продължение на работата."""
    msgs = [{"role": "user", "content": "намери снимките"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "a.jpg b.jpg"},
            {"role": "assistant", "content": "Намерих 2."},
            {"role": "user", "content": "а колко са големи общо?"}]
    assert mr.is_light_turn(msgs) is False


def test_a_fresh_plain_question_is_light(monkeypatch) -> None:
    monkeypatch.delenv("GENESIS_CHAT_ROUTING", raising=False)
    msgs = [{"role": "system", "content": "s"},
            {"role": "user", "content": "какво е рекурсия?"}]
    assert mr.is_light_turn(msgs) is True


def test_routing_can_be_switched_off(monkeypatch) -> None:
    monkeypatch.setenv("GENESIS_CHAT_ROUTING", "0")
    assert mr.is_light_turn([{"role": "user", "content": "здрасти"}]) is False


@pytest.mark.parametrize("text,calls,expected", [
    ("Рекурсията е...", None, False),
    ("", [{"id": "1"}], True),                       # native tool call
    ("[LIST_DIR: ~/Desktop]", None, True),           # text-tag tool call
    ("Error: цялата верига е изчерпана", None, True),
])
def test_escalation_when_the_small_model_reaches_for_a_tool(text, calls, expected) -> None:
    assert mr.light_reply_needs_escalation(text, calls) is expected

# ── Команда вместо модел ──────────────────────────────────────────────────

@pytest.mark.parametrize("text, cmd", [
    ("направи бекъп", "/backup"),
    ("Направи ми бекъп!", "/backup"),
    ("napravi backup", "/backup"),
    ("backup", "/backup"),
    ("бекъп сега", "/backup"),
    ("архивирай", "/backup"),
    ("make a backup", "/backup"),
    ("покажи уменията", "/skills"),
    ("какви умения имаш?", "/skills"),
    ("list skills", "/skills"),
    ("покажи моделите", "/models"),
    ("show models", "/models"),
    ("обнови се", "/update"),
    ("update yourself", "/update"),
])
def test_command_for_request_matches_whole_request(text, cmd):
    assert mr.command_for_request(text) == cmd


@pytest.mark.parametrize("text", [
    "направи бекъп на ~/projects",       # цел → моделът решава
    "как работи бекъпът?",               # въпрос за бекъпа, не заявка
    "направи бекъп и после деплой",     # повече от командата
    "покажи уменията за python код",
    "обнови README",
    "/backup",                          # вече е команда
    "",
    "здрасти",
    "backup " + "x" * 40,
])
def test_command_for_request_leaves_the_rest_to_the_model(text):
    assert mr.command_for_request(text) is None


# ── Команда вместо модел: „napravi backup" → /backup ─────────────────────────

@pytest.mark.parametrize("text,cmd", [
    ("napravi backup", "/backup"),
    ("Направи бекъп!", "/backup"),
    ("моля, направи бекъп", "/backup"),
    ("архивирай", "/backup"),
    ("обнови се", "/update"),
    ("има ли нова версия?", "/update"),
    ("check for updates", "/update"),
    ("покажи уменията", "/skills"),
    ("kakvi umeniq imash", "/skills"),
    ("покажи ми моделите", "/models"),
    ("смени модела", "/model"),
    ("изчисти разговора", "/clear"),
    ("нов разговор", "/clear"),
    ("покажи задачите", "/tasks"),
    ("помощ", "/help"),
])
def test_a_message_that_is_just_a_command_maps_to_it(text, cmd) -> None:
    assert mr.command_for_request(text) == cmd


@pytest.mark.parametrize("text", [
    "nameri i napravi backup genesis v disk D: v zip fail",   # реално — цел и формат
    "napravi backup na proekta",
    "направи бекъп на ~/proj",
    "backup the db to s3",
    "how do I make a backup?",
    "обнови",              # без обект — може да е продължение
    "обнови файла",
    "update",
    "clear",
    "help me fix this bug",
    "да",
    "/backup",             # вече е команда
    "",
])
def test_anything_more_than_the_intent_goes_to_the_model(text) -> None:
    assert mr.command_for_request(text) is None


def test_only_commands_that_change_something_ask_first() -> None:
    assert mr.CONFIRM_COMMANDS == {"/backup", "/clear"}
