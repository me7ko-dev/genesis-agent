"""Brain.complete — редът на веригата, резервите и формата на отговора.

Пазят поведението по разклонения, преди функцията да се цепи (C901 26).
Мрежа няма: `_call` и `_call_local` са подменени със сценарий.
"""
from __future__ import annotations

import pytest
import requests

from genesis_agent import brain as brain_mod
from genesis_agent.brain import Brain

HI = [{"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {"name": "READ_FILE", "parameters": {}}}]


def _entry(model: str, tools: bool = False) -> dict:
    return {"provider": "groq", "model": model, "size_b": 0, "supports_tools": tools}


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    brain_mod._EXHAUSTED.clear()
    monkeypatch.delenv("GENESIS_LOCAL_ONLY", raising=False)
    monkeypatch.setattr(brain_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(Brain, "_record_stat", lambda self, prov, lat, ok: None)
    monkeypatch.setattr(Brain, "_log_usage", lambda self: None)
    monkeypatch.setattr("genesis_agent.provider_stats.deprioritize_flaky", lambda chain: chain)
    yield
    brain_mod._EXHAUSTED.clear()


def _brain(monkeypatch, chain: list[dict], outcomes: dict[str, list], *, local=None,
           local_hit=None, local_error="ollama няма"):
    calls: list[tuple] = []
    local_calls: list[list] = []

    def _fake_call(self, provider, model, messages, tools=None, extra=None):
        calls.append((model, bool(tools), messages))
        result = outcomes[model].pop(0) if outcomes.get(model) else RuntimeError("HTTP_500: x")
        if isinstance(result, Exception):
            raise result
        return result, None

    def _fake_local(self, messages, attempts=1):
        local_calls.append(messages)
        self._last_local_error = local_error
        return local_hit

    monkeypatch.setattr(Brain, "_call", _fake_call)
    monkeypatch.setattr(Brain, "_call_local", _fake_local)
    b = Brain.__new__(Brain)
    b.chain, b.local, b._pinned, b.current = chain, local, None, None
    b._last_usage, b._last_local_error = None, None
    return b, calls, local_calls


def test_no_models_at_all(monkeypatch) -> None:
    b, _, _ = _brain(monkeypatch, [], {})
    r = b.complete(HI)
    assert r.raw_text.startswith("Error: няма конфигурирани модели") and r.tool_calls is None


class TestLocalOnly:
    def test_without_a_local_model(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_LOCAL_ONLY", "1")
        b, calls, _ = _brain(monkeypatch, [_entry("a")], {"a": ["облак"]})
        assert b.complete(HI).raw_text == "Error: локален режим — няма наличен локален модел"
        assert calls == []

    def test_with_tools_gets_the_tag_docs(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_LOCAL_ONLY", "1")
        b, calls, local_calls = _brain(monkeypatch, [_entry("a")], {"a": ["облак"]},
                                       local={"provider": "ollama_local", "model": "m"},
                                       local_hit=("локално", "x = 1"))
        r = b.complete(HI, tools=TOOLS)
        assert (r.raw_text, r.code, r.tool_calls) == ("локално", "x = 1", None)
        assert calls == []
        assert local_calls == [Brain._with_tool_tag_docs(Brain._sanitize_for_textmode(HI))]

    def test_a_local_miss_says_why(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_LOCAL_ONLY", "1")
        b, _, local_calls = _brain(monkeypatch, [], {},
                                   local={"provider": "ollama_local", "model": "m"})
        assert b.complete(HI).raw_text == "Error: локален режим — ollama няма"
        assert local_calls == [HI]


class TestChainOrder:
    def test_tool_capable_first_the_rest_in_text_mode(self, monkeypatch) -> None:
        b, calls, _ = _brain(monkeypatch, [_entry("plain"), _entry("native", True)],
                             {"native": [RuntimeError("HTTP_400: x")], "plain": ["ok"]})
        assert b.complete(HI, tools=TOOLS).raw_text == "ok"
        assert [(m, t) for m, t, _ in calls] == [("native", True), ("plain", False)]
        assert calls[0][2] == HI
        assert calls[1][2] == Brain._with_tool_tag_docs(Brain._sanitize_for_textmode(HI))
        assert b.current["model"] == "plain"

    def test_pinned_goes_first_even_without_tools(self, monkeypatch) -> None:
        chain = [_entry("a", True), _entry("pinned")]
        b, calls, _ = _brain(monkeypatch, chain, {"pinned": ["ok"]})
        b._pinned = chain[1]
        assert b.complete(HI, tools=TOOLS).raw_text == "ok"
        assert [m for m, _, _ in calls] == ["pinned"]

    def test_avoid_skips_that_model(self, monkeypatch) -> None:
        b, calls, _ = _brain(monkeypatch, [_entry("a"), _entry("b")], {"a": ["A"], "b": ["B"]})
        assert b.complete(HI, avoid=("groq", "a")).raw_text == "B"
        assert [m for m, _, _ in calls] == ["b"]

    def test_flaky_stats_failing_keeps_the_order(self, monkeypatch) -> None:
        def _boom(chain):
            raise ValueError("повредена статистика")
        monkeypatch.setattr("genesis_agent.provider_stats.deprioritize_flaky", _boom)
        b, calls, _ = _brain(monkeypatch, [_entry("a"), _entry("b")], {"a": ["A"]})
        assert b.complete(HI).raw_text == "A"
        assert [m for m, _, _ in calls] == ["a"]


@pytest.mark.parametrize("text, code", [
    ("Ето:\n```python\nx = 1\n```\nготово", "x = 1"),
    ("  def f():\n    return 1", "  def f():\n    return 1"),
    ("само думи", ""),
])
def test_code_is_taken_from_the_reply(monkeypatch, text, code) -> None:
    b, _, _ = _brain(monkeypatch, [_entry("a")], {"a": [text]})
    r = b.complete(HI)
    assert (r.raw_text, r.code, r.tool_calls) == (text, code, None)


class TestFailures:
    def test_network_error_moves_on(self, monkeypatch) -> None:
        b, calls, _ = _brain(monkeypatch, [_entry("a"), _entry("b")],
                             {"a": [requests.exceptions.ConnectionError("refused")], "b": ["B"]})
        assert b.complete(HI).raw_text == "B"
        assert [m for m, _, _ in calls] == ["a", "b"]

    def test_429_cools_the_model_down(self, monkeypatch) -> None:
        b, calls, _ = _brain(monkeypatch, [_entry("a"), _entry("b")],
                             {"a": [RuntimeError("HTTP_429: rate")], "b": ["B", "B"]})
        b.complete(HI)
        b.complete(HI)
        assert [m for m, _, _ in calls] == ["a", "b", "b"]

    def test_whole_chain_down_then_the_local_fallback(self, monkeypatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(brain_mod.time, "sleep", sleeps.append)
        b, calls, local_calls = _brain(
            monkeypatch, [_entry("a", True)], {"a": [RuntimeError("HTTP_400: bad")] * 2},
            local={"provider": "ollama_local", "model": "m"}, local_hit=("локално", ""))
        r = b.complete(HI, tools=TOOLS)
        assert r.raw_text == "локално" and r.tool_calls is None
        assert len(calls) == brain_mod.RETRY_ROUNDS and sleeps == [8] * (brain_mod.RETRY_ROUNDS - 1)
        assert local_calls == [Brain._with_tool_tag_docs(Brain._sanitize_for_textmode(HI))]

    def test_local_miss_reports_the_local_error(self, monkeypatch) -> None:
        b, _, _ = _brain(monkeypatch, [_entry("a")], {"a": [RuntimeError("HTTP_400: bad")] * 2},
                         local={"provider": "ollama_local", "model": "m"})
        assert b.complete(HI).raw_text == "Error: цялата верига е изчерпана | последна: ollama няма"

    def test_avoided_local_is_not_asked(self, monkeypatch) -> None:
        b, _, local_calls = _brain(monkeypatch, [_entry("a")],
                                   {"a": [RuntimeError("HTTP_400: bad")] * 2},
                                   local={"provider": "ollama_local", "model": "m"})
        r = b.complete(HI, avoid=("ollama_local", "m"))
        assert r.raw_text == "Error: цялата верига е изчерпана | последна: HTTP_400: bad"
        assert local_calls == []
