"""genesis_terminal_agent.call_openai_compatible — truncation guard (2026-08-12).

This is the "legacy" direct HTTP path (_ask_via_legacy), reached only when the
operator manually picks a provider Brain doesn't know about (gemini/github/
openai/llmstudio via the `/model` menu) — a narrow escape hatch, but a real,
reachable one, and it duplicated genesis_agent.brain._http's exact same gap:
finish_reason was never read, so a response cut off mid code-fence at the
max_tokens ceiling came back looking like a normal, complete answer.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import genesis_terminal_agent as gta


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None) -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = ""

    def json(self):
        return self._json_body


@pytest.fixture(autouse=True)
def _openai_provider(monkeypatch):
    monkeypatch.setitem(gta.KEYS, "OPENAI_API_KEY", "test-key")


def test_truncated_response_raises_instead_of_returning_broken_code(monkeypatch) -> None:
    resp = _FakeResponse(200, {
        "choices": [{
            "message": {"content": "sure:\n```python\ndef solve():\n    x = ["},
            "finish_reason": "length",
        }],
    })
    monkeypatch.setattr(gta.requests, "post", lambda *a, **kw: resp)
    with pytest.raises(RuntimeError, match="HTTP_TRUNCATED"):
        gta.call_openai_compatible([{"role": "user", "content": "hi"}], "openai", "gpt-4")


def test_complete_response_with_length_finish_is_returned(monkeypatch) -> None:
    """Hitting the ceiling right after the closing fence is harmless."""
    resp = _FakeResponse(200, {
        "choices": [{
            "message": {"content": "```python\ndef f():\n    pass\n```"},
            "finish_reason": "length",
        }],
    })
    monkeypatch.setattr(gta.requests, "post", lambda *a, **kw: resp)
    content, _ = gta.call_openai_compatible([{"role": "user", "content": "hi"}], "openai", "gpt-4")
    assert "def f()" in content


def test_normal_stop_is_returned_unchanged(monkeypatch) -> None:
    resp = _FakeResponse(200, {
        "choices": [{
            "message": {"content": "just a plain answer"},
            "finish_reason": "stop",
        }],
    })
    monkeypatch.setattr(gta.requests, "post", lambda *a, **kw: resp)
    content, _ = gta.call_openai_compatible([{"role": "user", "content": "hi"}], "openai", "gpt-4")
    assert content == "just a plain answer"


class TestRestoreSession:
    """`/history` used to do a bare `messages = json.load(f)` (fixed 2026-08-12).

    Two distinct breakages: the live `messages` structure is a
    deque(maxlen=_HISTORY_MAXLEN) everywhere else, so loading a session
    silently replaced it with an unbounded plain list; and since a bounded
    deque evicts from the FRONT, a long restored session would drop exactly
    the system message — taking env_facts and the workspace briefing with it,
    and also quietly disabling compact_chat_history, which bails out unless
    messages[0] is the system role.
    """

    def test_returns_a_bounded_deque_not_a_list(self) -> None:
        restored = gta._restore_session(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            "fallback",
        )
        assert restored.maxlen == gta._HISTORY_MAXLEN
        assert not isinstance(restored, list)

    def test_system_message_survives_an_oversized_session(self) -> None:
        loaded = [{"role": "system", "content": "STALE PROMPT"}]
        loaded += [{"role": "user", "content": f"msg{i}"} for i in range(100)]
        restored = gta._restore_session(loaded, "CURRENT PROMPT")

        assert len(restored) == gta._HISTORY_MAXLEN
        assert restored[0]["role"] == "system"
        # and it kept the most RECENT turns, not the oldest
        assert restored[-1]["content"] == "msg99"

    def test_the_current_prompt_replaces_the_saved_one(self) -> None:
        """Deliberate, and shared with the GUI via agent_core.restored_history:
        the live SYSTEM_PROMPT carries THIS session's env_facts and workspace
        briefing. Restoring the one serialized last week would hand the model
        a stale briefing and possibly stale paths."""
        loaded = [
            {"role": "system", "content": "STALE PROMPT FROM LAST WEEK"},
            {"role": "user", "content": "hi"},
        ]
        restored = gta._restore_session(loaded, "CURRENT PROMPT")
        assert restored[0]["content"] == "CURRENT PROMPT"

    def test_session_without_a_system_message_gets_the_current_prompt(self) -> None:
        restored = gta._restore_session([{"role": "user", "content": "hi"}], "CURRENT PROMPT")
        assert restored[0]["role"] == "system"
        assert restored[0]["content"] == "CURRENT PROMPT"

    def test_short_session_keeps_every_conversational_turn(self) -> None:
        loaded = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        restored = list(gta._restore_session(loaded, "CURRENT PROMPT"))
        assert restored[0] == {"role": "system", "content": "CURRENT PROMPT"}
        assert restored[1:] == loaded[1:]

    def test_duplicate_system_messages_collapse_to_one(self) -> None:
        """compact_chat_history injects a second system message (the summary),
        so a restored file can legitimately contain more than one — the result
        must still carry exactly one, at the head."""
        loaded = [
            {"role": "system", "content": "original"},
            {"role": "system", "content": "## Резюме на по-ранния разговор:\n..."},
            {"role": "user", "content": "hi"},
        ]
        restored = gta._restore_session(loaded, "CURRENT PROMPT")
        assert restored[0]["content"] == "CURRENT PROMPT"
        assert sum(1 for m in restored if m.get("role") == "system") == 1


class TestTerminalHasTheSameIntegrityCheckAsTheSharedCore:
    """The terminal is the DEFAULT frontend (`genesis`) but runs its own tool
    loop, separate from agent_core.run_tool_loop. claim_check was wired into
    the shared core only, which left the most-used entry point with no check
    against simulated work at all. These pin the two together so they cannot
    drift apart again silently.
    """

    def test_the_terminal_module_wires_in_claim_check(self) -> None:
        import genesis_terminal_agent as gta
        src = Path(gta.__file__).read_text(encoding="utf-8")
        assert "claim_check.unsupported_claims" in src, (
            "терминалният цикъл трябва да проверява твърденията, както ядрото")
        assert "claim_check.nudge_text" in src

    def test_both_frontends_share_one_text_result_parser(self) -> None:
        """Доказателството за claim_check се вади от формата на резултата
        (`[RUN_CMD: ...]`). Този разбор живее в claim_check и се ползва и от
        двата цикъла — по-рано беше копиран дословно и на двете места, което
        значи, че промяна във формата ги обезоръжава едновременно и мълчаливо.
        Затова тук се проверява самата функция, а не текстът на модула."""
        from genesis_agent import claim_check
        results = [
            "[RUN_CMD: pip install ruff]\nSuccessfully installed",
            "[RUN_CMD: rm -rf /]\n[SANDBOX BLOCKED] катастрофално",
            "без разпознаваем префикс",
        ]
        executed = claim_check.executed_from_text_results(results)
        assert ("RUN_CMD", "pip install ruff") in executed
        assert not any("rm -rf" in args for _n, args in executed), (
            "блокирана команда не е изпълнение"
        )
        assert len(executed) == 1

        for module in ("genesis_terminal_agent", "genesis_agent.agent_core"):
            src = Path(__import__(module, fromlist=["x"]).__file__).read_text(
                encoding="utf-8")
            assert "executed_from_text_results" in src, f"{module} не ползва общия разбор"

    def test_the_shared_core_still_has_it_too(self) -> None:
        from genesis_agent import agent_core
        src = Path(agent_core.__file__).read_text(encoding="utf-8")
        assert "claim_check.unsupported_claims" in src


class TestTheTerminalRoutesThroughBrainWhenItCan:
    """Терминалът има собствен, по-стар път към доставчиците — escape hatch за
    доставчик, който Brain не знае. Кои са те беше ТВЪРД списък, а той се
    разминава с това, което описва: `gemini` влезе в brain.py с този клон, а
    остана изброен като „непознат", тоест избор на Gemini в `/model` тихо
    заобикаляше кеширането на промпта, cooldown-а при 429/402/503,
    деприоритизацията на болни доставчици и общото отчитане.

    Сега се пита самият `brain._PROVIDERS`, а тестът пази двете страни да не се
    разминат отново.
    """

    def test_a_provider_brain_knows_goes_through_brain(self) -> None:
        import genesis_terminal_agent as t
        from genesis_agent.brain import _PROVIDERS
        for name in sorted(_PROVIDERS):
            assert t._brain_handles(name), f"{name} е в _PROVIDERS, но отива по стария път"

    def test_gemini_and_openai_specifically(self) -> None:
        """Двете, които бяха в твърдия списък по погрешка."""
        import genesis_terminal_agent as t
        assert t._brain_handles("gemini")
        assert t._brain_handles("openai")

    def test_the_terminal_only_names_are_really_unknown_to_brain(self) -> None:
        """Обратната посока: ако Brain научи някое от тези имена, списъкът
        трябва да се смали, а не да остане да ги отклонява."""
        import genesis_terminal_agent as t
        from genesis_agent.brain import _PROVIDERS
        for name in sorted(t._TERMINAL_ONLY_PROVIDERS):
            assert name not in _PROVIDERS, (
                f"Brain вече знае {name!r} — махни го от _TERMINAL_ONLY_PROVIDERS")

    def test_an_unknown_name_falls_back_instead_of_crashing(self) -> None:
        import genesis_terminal_agent as t
        assert t._brain_handles("нещо-което-никой-не-знае") is False

    def test_a_broken_brain_import_does_not_take_the_terminal_down(self, monkeypatch) -> None:
        """Терминалът е фронтендът по подразбиране: счупен внос на brain трябва
        да го прати по стария път, не да го убие."""
        import builtins

        import genesis_terminal_agent as t
        real_import = builtins.__import__

        def _boom(name, *a, **kw):
            if name == "genesis_agent.brain":
                raise ImportError("нарочно")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _boom)
        assert t._brain_handles("gemini") is False
