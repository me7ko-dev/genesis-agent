"""genesis_terminal_agent.call_openai_compatible — truncation guard (2026-08-12).

This is the "legacy" direct HTTP path (_ask_via_legacy), reached only when the
operator manually picks a provider Brain doesn't know about (gemini/github/
openai/llmstudio via the `/model` menu) — a narrow escape hatch, but a real,
reachable one, and it duplicated genesis_agent.brain._http's exact same gap:
finish_reason was never read, so a response cut off mid code-fence at the
max_tokens ceiling came back looking like a normal, complete answer.
"""
from __future__ import annotations

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
