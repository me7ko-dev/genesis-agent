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
