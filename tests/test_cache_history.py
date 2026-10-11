"""budget.cache_history и пътят към native доставчика — историята само се
допълва, като в Claude Code (2026-10-11).

Измерено преди: bench egn-check с claude-opus-5, 24 заявки — в кеша само
системата, 218 849 от 315 468 входни токена платени изцяло ($1.60 на пуск).
Причината: budget_history свива още един стар резултат на всеки рунд, а
прозорецът се плъзга с по едно — префиксът се сменя всеки път.
"""
from __future__ import annotations

from itertools import pairwise

import pytest

from genesis_agent import brain
from genesis_agent.budget import budget_history, cache_history


def _rounds(n: int, size: int = 50) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "задачата"}]
    for i in range(n):
        out.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "RUN_CMD", "arguments": "{}"}}]})
        out.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"изход {i} " + "x" * size})
    return out


class TestCacheHistory:
    def test_under_the_ceiling_nothing_changes(self) -> None:
        msgs = _rounds(10, size=6000)
        assert cache_history(msgs, max_chars=10**6) == msgs

    def test_old_results_are_not_shrunk_like_budget_history_does(self) -> None:
        msgs = _rounds(6, size=6000)
        assert len(cache_history(msgs, max_chars=10**6)[2 + 1]["content"]) > 6000
        assert len(budget_history(msgs)[2 + 1]["content"]) < 6000

    def test_over_the_ceiling_whole_chunks_go_and_the_prefix_holds_between_jumps(self) -> None:
        """Стабилност между рундовете: два последователни рунда без скок на
        парчето имат еднакво начало — точно това чете кешът."""
        limit, chunk = 40_000, 10
        prefixes = []
        for n in range(30, 60):
            out = cache_history(_rounds(n, size=1000), max_chars=limit, chunk=chunk)
            assert out[0]["content"] == "SYS" and out[1]["content"] == "задачата"
            assert sum(len(str(m.get("content") or "")) for m in out) <= limit + 2000
            prefixes.append(out[:6])
        changes = sum(1 for a, b in pairwise(prefixes) if a != b)
        assert changes <= 30 // (chunk // 2) + 1, changes   # скокове, не всеки рунд

    def test_a_cut_never_starts_with_a_tool_result_without_its_call(self) -> None:
        for n in range(30, 50):
            out = cache_history(_rounds(n, size=1000), max_chars=30_000, chunk=7)
            assert out[2]["role"] != "tool"

    def test_the_task_stays_even_when_its_chunk_is_dropped(self) -> None:
        out = cache_history(_rounds(80, size=1000), max_chars=20_000, chunk=10)
        assert [m["content"] for m in out[:2]] == ["SYS", "задачата"]


class TestNativeProviderGetsTheAppendOnlyHistory:
    @pytest.fixture
    def seen(self, monkeypatch) -> dict:
        got: dict = {}

        def fake_call(self, provider, model, messages, tools=None):
            got[provider] = messages
            return "готово", None

        monkeypatch.setattr(brain.Brain, "_call", fake_call)
        monkeypatch.setattr(brain, "_local_available", lambda m: False)
        monkeypatch.setattr("genesis_agent.provider_stats.deprioritize_flaky", lambda c: c)
        monkeypatch.setattr(brain, "_EXHAUSTED", {})
        return got

    @staticmethod
    def _run(monkeypatch, provider: str) -> None:
        monkeypatch.setattr(brain, "_load_chain", lambda: [
            {"provider": provider, "model": "m", "size_b": 120, "supports_tools": True}])
        tools = [{"type": "function", "function": {"name": "RUN_CMD", "description": "d",
                                                   "parameters": {"type": "object", "properties": {}}}}]
        brain.Brain(use_local=False).complete(_rounds(6, size=6000), tools=tools)

    def test_anthropic_sees_full_old_results_a_free_provider_sees_them_shrunk(
            self, monkeypatch, seen) -> None:
        self._run(monkeypatch, "anthropic")
        self._run(monkeypatch, "groq")
        old = next(m for m in seen["anthropic"] if m.get("role") == "tool")
        assert len(old["content"]) > 6000
        old_free = next(m for m in seen["groq"] if m.get("role") == "tool")
        assert len(old_free["content"]) < 6000


def test_the_request_caches_the_tail_and_the_system_for_an_hour(monkeypatch) -> None:
    import sys
    import types

    sent: dict = {}

    class _Usage:
        input_tokens, output_tokens = 10, 2
        cache_read_input_tokens = cache_creation_input_tokens = 0

    class _Text:
        type, text = "text", "ok"

    class _Messages:
        def create(self, **params):
            sent.update(params)
            return types.SimpleNamespace(stop_reason="end_turn", content=[_Text()], usage=_Usage())

    fake = types.ModuleType("anthropic")
    for name in ("BadRequestError", "AuthenticationError", "PermissionDeniedError",
                 "RateLimitError", "APIStatusError", "APIConnectionError"):
        setattr(fake, name, type(name, (Exception,), {"status_code": 500}))
    fake.Anthropic = lambda **_kw: types.SimpleNamespace(
        beta=types.SimpleNamespace(messages=_Messages()), messages=_Messages())
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    brain.Brain()._call_anthropic("k", "claude-opus-5", _rounds(3), None, "high")
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_light_work_does_not_take_the_paid_chain_from_the_environment(monkeypatch) -> None:
    """Резюмето, паметта и графът на знанието (Brain(light=True)) не бива да
    тръгват към claude-opus-5 само защото операторът е пуснал GENESIS_QUALITY=max."""
    monkeypatch.setenv("GENESIS_QUALITY", "max")
    monkeypatch.setattr(brain, "_load_premium_chain", lambda: [
        {"provider": "anthropic", "model": "claude-opus-5", "size_b": 0,
         "supports_tools": True, "premium": True, "effort": "xhigh"}])
    monkeypatch.setattr(brain.Brain, "_provider_key", lambda self, env: "k")
    assert brain.Brain(use_local=False).premium
    assert not brain.Brain(light=True, use_local=False).premium
    assert brain.Brain(light=True, use_local=False, quality="max").premium
