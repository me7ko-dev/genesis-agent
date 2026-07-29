"""genesis_agent.brain — pure-logic pieces that don't need a live provider:
trim_round_history (token-savings for multi-round missions), _provider_key
(one-key-per-provider lookup), and _http's response handling (the exact
KeyError-on-200-with-no-choices bug found live in the OpenRouter forge test,
2026-07-25 — see the docstring in brain.py)."""
from __future__ import annotations

import pytest

from genesis_agent.brain import Brain


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None, text: str = "") -> None:
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


class TestTrimRoundHistory:
    def test_short_history_unchanged(self) -> None:
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        assert Brain.trim_round_history(messages) == messages

    def test_keeps_system_goal_and_last_exchange(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "goal"},
            {"role": "assistant", "content": "attempt 1"},
            {"role": "user", "content": "error 1"},
            {"role": "assistant", "content": "attempt 2"},
            {"role": "user", "content": "error 2"},
        ]
        trimmed = Brain.trim_round_history(messages)
        assert trimmed == [messages[0], messages[1], messages[4], messages[5]]

    def test_does_not_split_a_tool_call_group(self) -> None:
        """A naive last-2 slice would cut off the assistant(tool_calls) parent
        and send an orphaned role='tool' message — breaks the OpenAI schema."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "goal"},
            {"role": "assistant", "content": "old"},
            {"role": "user", "content": "old error"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "result"},
        ]
        trimmed = Brain.trim_round_history(messages)
        assert trimmed == [messages[0], messages[1], messages[4], messages[5]]

    def test_normal_non_tool_round_is_byte_identical_to_before(self) -> None:
        """Orchestrator/project_builder never produce role='tool' messages —
        the tool-call guard must never fire for them."""
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "goal"},
            {"role": "assistant", "content": "code v1"},
            {"role": "user", "content": "traceback v1"},
            {"role": "assistant", "content": "code v2"},
            {"role": "user", "content": "traceback v2"},
        ]
        trimmed = Brain.trim_round_history(messages)
        assert trimmed == messages[:2] + messages[-2:]


class TestProviderKey:
    def _brain_with_keys(self, keys: dict) -> Brain:
        b = Brain.__new__(Brain)
        b.keys = keys
        return b

    def test_returns_key_when_present(self) -> None:
        b = self._brain_with_keys({"HF_TOKEN": "abc123"})
        assert b._provider_key("HF_TOKEN") == "abc123"

    def test_returns_none_when_absent(self) -> None:
        b = self._brain_with_keys({})
        assert b._provider_key("HF_TOKEN") is None

    def test_strips_whitespace(self) -> None:
        b = self._brain_with_keys({"HF_TOKEN": "  abc123  "})
        assert b._provider_key("HF_TOKEN") == "abc123"

    def test_blank_key_treated_as_absent(self) -> None:
        b = self._brain_with_keys({"HF_TOKEN": "   "})
        assert b._provider_key("HF_TOKEN") is None


class TestHttp:
    def _brain(self) -> Brain:
        return Brain.__new__(Brain)

    def test_valid_response_returns_content_and_records_usage(self, monkeypatch) -> None:
        b = self._brain()
        resp = _FakeResponse(200, {
            "choices": [{"message": {"content": "hi there"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        })
        monkeypatch.setattr("genesis_agent.brain.requests.post", lambda *a, **kw: resp)
        content, tool_calls = b._http("https://x", "key", "m", [], 30)
        assert content == "hi there"
        assert tool_calls is None
        assert b._last_usage == {"prompt_tokens": 5, "completion_tokens": 3}

    def test_non_200_raises_runtime_error_with_status_code(self, monkeypatch) -> None:
        b = self._brain()
        resp = _FakeResponse(429, text="rate limited")
        monkeypatch.setattr("genesis_agent.brain.requests.post", lambda *a, **kw: resp)
        with pytest.raises(RuntimeError, match="HTTP_429"):
            b._http("https://x", "key", "m", [], 30)

    def test_200_without_choices_raises_malformed_not_keyerror(self, monkeypatch) -> None:
        """The exact live bug (2026-07-25): a provider (OpenRouter) returned
        200 OK with no 'choices' key. A raw KeyError there escaped the
        fallback loop's `except RuntimeError` and crashed the whole mission
        instead of falling through to the next model."""
        b = self._brain()
        resp = _FakeResponse(200, {"error": "upstream failure"})
        monkeypatch.setattr("genesis_agent.brain.requests.post", lambda *a, **kw: resp)
        with pytest.raises(RuntimeError, match="HTTP_200_MALFORMED"):
            b._http("https://x", "key", "m", [], 30)

    def test_empty_content_and_no_tool_calls_raises(self, monkeypatch) -> None:
        b = self._brain()
        resp = _FakeResponse(200, {"choices": [{"message": {"content": ""}}]})
        monkeypatch.setattr("genesis_agent.brain.requests.post", lambda *a, **kw: resp)
        with pytest.raises(RuntimeError):
            b._http("https://x", "key", "m", [], 30)

    def test_tool_calls_returned_alongside_empty_content(self, monkeypatch) -> None:
        b = self._brain()
        resp = _FakeResponse(200, {
            "choices": [{"message": {"content": None, "tool_calls": [{"id": "1"}]}}],
        })
        monkeypatch.setattr("genesis_agent.brain.requests.post", lambda *a, **kw: resp)
        content, tool_calls = b._http("https://x", "key", "m", [], 30, tools=[{}])
        assert content == ""
        assert tool_calls == [{"id": "1"}]
