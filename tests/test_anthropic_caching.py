"""genesis_agent.brain._call_anthropic — формата на заявката, която реално
тръгва към Anthropic. Досега се тестваше само чистият превод (виж
test_anthropic_translation.py); самото сглобяване на заявката — не.

Защо е важно точно тук: схемите на инструментите (~10 100 символа) и
системният промпт (~8 500) се изпращат наново при ВСЯКО обръщение, а таванът
на рундовете е 25. Prompt caching ги прави евтини, но е съвпадение по
ПРЕФИКС — ако точката на кеша липсва или префиксът се разваля, всичко
продължава да работи и просто струва пълна цена. Тоест провалът е безшумен:
единственото, което го хваща, е проверка на изпратената заявка и на
`cache_read_input_tokens` в отговора.
"""
from __future__ import annotations

import sys
import types

import pytest

from genesis_agent.brain import Brain


class _FakeUsage:
    input_tokens = 1200
    output_tokens = 80
    cache_read_input_tokens = 4300
    cache_creation_input_tokens = 0


class _TextBlock:
    type = "text"
    text = "готово"


class _FakeResponse:
    def __init__(self, usage) -> None:
        self.stop_reason = "end_turn"
        self.content = [_TextBlock()]
        self.usage = usage


class _Recorder:
    """Улавя параметрите, с които е извикан клиентът."""

    def __init__(self) -> None:
        self.params: dict = {}
        self.usage: object = _FakeUsage()

    def create(self, **params):
        self.params = params
        return _FakeResponse(self.usage)


@pytest.fixture
def sent(monkeypatch):
    """Подменя пакета `anthropic` с фалшив и връща записаните параметри."""
    recorder = _Recorder()

    fake = types.ModuleType("anthropic")

    class _Err(Exception):
        pass

    class _StatusErr(Exception):
        status_code = 500

    fake.BadRequestError = type("BadRequestError", (_Err,), {})
    fake.AuthenticationError = type("AuthenticationError", (_Err,), {})
    fake.PermissionDeniedError = type("PermissionDeniedError", (_Err,), {})
    fake.RateLimitError = type("RateLimitError", (_Err,), {})
    fake.APIStatusError = type("APIStatusError", (_StatusErr,), {})
    fake.APIConnectionError = type("APIConnectionError", (_Err,), {})

    class _Client:
        def __init__(self, **_kw) -> None:
            self.beta = types.SimpleNamespace(messages=recorder)
            self.messages = recorder

    fake.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return recorder


MESSAGES = [
    {"role": "system", "content": "ти си Genesis"},
    {"role": "user", "content": "направи нещо"},
]

TOOLS = [{"type": "function", "function": {
    "name": "READ_FILE", "description": "чете файл",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]


def _call(brain: Brain, sent, messages=None, tools=None):
    return brain._call_anthropic("key", "claude-opus-5",
                                 messages if messages is not None else MESSAGES,
                                 tools if tools is not None else TOOLS, "high")


class TestTheRequestCarriesACacheBreakpoint:
    def test_the_system_prompt_is_sent_as_a_cached_block(self, sent) -> None:
        _call(Brain(), sent)
        system = sent.params["system"]
        assert isinstance(system, list), "суров низ не може да носи cache_control"
        assert system[0]["type"] == "text"
        assert system[0]["text"] == "ти си Genesis"
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_the_tools_are_still_sent_and_stay_before_the_breakpoint(self, sent) -> None:
        """Редът на рендиране е tools → system → messages, затова точката на
        последния системен блок покрива и схемите. Тестът пази точно това: че
        инструментите пътуват в същата заявка."""
        _call(Brain(), sent)
        assert [t["name"] for t in sent.params["tools"]] == ["READ_FILE"]

    def test_no_breakpoint_is_placed_inside_the_conversation(self, sent) -> None:
        """budget.budget_history свива по-старите резултати с напредването на
        рундовете — байтовете в средата на историята се менят между заявките.
        Точка там би се разминавала почти винаги и само би харчила запис."""
        _call(Brain(), sent, messages=[
            {"role": "system", "content": "s"},
            {"role": "user", "content": "x"},
            {"role": "assistant", "content": "y"},
            {"role": "user", "content": "z"},
        ])
        for msg in sent.params["messages"]:
            content = msg["content"]
            if isinstance(content, list):
                assert all("cache_control" not in b for b in content), msg

    def test_without_a_system_prompt_nothing_is_marked(self, sent) -> None:
        _call(Brain(), sent, messages=[{"role": "user", "content": "само въпрос"}])
        assert "system" not in sent.params


class TestCacheTokensAreReportedNotAssumed:
    def test_cache_reads_are_recorded_separately_from_fresh_input(self, sent) -> None:
        brain = Brain()
        _call(brain, sent)
        assert brain._last_usage == {
            "prompt_tokens": 1200,
            "completion_tokens": 80,
            "cached_read_tokens": 4300,
            "cached_write_tokens": 0,
        }

    def test_a_provider_without_cache_fields_reports_zero_not_an_error(self, sent) -> None:
        class _BareUsage:
            input_tokens = 10
            output_tokens = 2

        sent.usage = _BareUsage()
        brain = Brain()
        _call(brain, sent)
        assert brain._last_usage["cached_read_tokens"] == 0
        assert brain._last_usage["cached_write_tokens"] == 0


class TestParametersMatchWhatTheModelAccepts:
    """`output_config.effort` и adaptive thinking съществуват от поколение 4.6
    нагоре. На Haiku 4.5 и по-старите `effort` връща 400 — а именно към тях
    посяга човек, който иска да плаща по-малко. Пращахме ги безусловно, тоест
    смяната на модела в config.yaml даваше HTTP 400 без обяснение."""

    def test_a_current_model_gets_adaptive_thinking_and_effort(self, sent) -> None:
        Brain()._call_anthropic("k", "claude-opus-5", MESSAGES, TOOLS, "high")
        assert sent.params["thinking"] == {"type": "adaptive"}
        assert sent.params["output_config"] == {"effort": "high"}

    def test_sonnet_5_is_treated_as_current_too(self, sent) -> None:
        Brain()._call_anthropic("k", "claude-sonnet-5", MESSAGES, TOOLS, "")
        assert "thinking" in sent.params
        assert sent.params["output_config"]["effort"] == "xhigh"   # ANTHROPIC_EFFORT

    def test_haiku_gets_neither_instead_of_a_400(self, sent) -> None:
        Brain()._call_anthropic("k", "claude-haiku-4-5", MESSAGES, TOOLS, "high")
        assert "thinking" not in sent.params
        assert "output_config" not in sent.params

    def test_an_unknown_model_falls_back_to_the_accepted_everywhere_shape(self, sent) -> None:
        """Имената се менят по-често от този файл — непознато име трябва да
        даде работеща заявка, а не отказана."""
        Brain()._call_anthropic("k", "claude-future-9", MESSAGES, TOOLS, "high")
        assert "output_config" not in sent.params
        assert sent.params["messages"], "заявката пак тръгва"

    def test_the_system_prompt_is_still_cached_on_every_model(self, sent) -> None:
        Brain()._call_anthropic("k", "claude-haiku-4-5", MESSAGES, TOOLS, "high")
        assert sent.params["system"][0]["cache_control"] == {"type": "ephemeral"}
