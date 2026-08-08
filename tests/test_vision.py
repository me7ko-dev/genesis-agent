"""genesis_agent.vision — describe_screen(), the backend for [LOOK_AT_SCREEN]
dispatched from genesis_skills.py. Zero coverage before this file.

The module's stated safety property is what matters most: screen capture is
OFF by default (GENESIS_VISION_ENABLED must be explicitly "1") because
whatever is on screen may be sensitive — that gate is the first thing tested,
before any of the capture/vision-model plumbing.
"""
from __future__ import annotations

import pytest

import genesis_agent.vision as vs


class _FakeResponse:
    def __init__(self, status_code: int, content: str | None = None) -> None:
        self.status_code = status_code
        self._content = content

    def json(self):
        return {"message": {"content": self._content}} if self._content is not None else {}


class TestVisionEnabled:
    def test_disabled_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("GENESIS_VISION_ENABLED", raising=False)
        assert vs.vision_enabled() is False

    def test_enabled_only_with_exact_value_1(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_VISION_ENABLED", "1")
        assert vs.vision_enabled() is True
        monkeypatch.setenv("GENESIS_VISION_ENABLED", "true")
        assert vs.vision_enabled() is False
        monkeypatch.setenv("GENESIS_VISION_ENABLED", "yes")
        assert vs.vision_enabled() is False


class TestDescribeScreenDisabled:
    def test_disabled_returns_privacy_message_without_capturing(self, monkeypatch) -> None:
        monkeypatch.delenv("GENESIS_VISION_ENABLED", raising=False)
        called = []
        monkeypatch.setattr(vs, "capture_screen", lambda: called.append(True))
        out = vs.describe_screen("what's on screen")
        assert "ИЗКЛЮЧЕНО" in out
        assert called == []


@pytest.fixture
def _enabled(monkeypatch):
    monkeypatch.setenv("GENESIS_VISION_ENABLED", "1")


class TestDescribeScreenEnabled:
    def test_capture_failure_is_reported_not_raised(self, monkeypatch, _enabled) -> None:
        def _boom():
            raise RuntimeError("no display")
        monkeypatch.setattr(vs, "capture_screen", _boom)
        out = vs.describe_screen("q")
        assert "Грешка при screenshot" in out
        assert "no display" in out

    def test_successful_response_returns_content(self, monkeypatch, _enabled) -> None:
        monkeypatch.setattr(vs, "capture_screen", lambda: b"fake-png-bytes")
        monkeypatch.setattr(vs.requests, "post",
                            lambda *a, **kw: _FakeResponse(200, "a browser window is open"))
        out = vs.describe_screen("what's on screen")
        assert out == "a browser window is open"

    def test_empty_content_returns_placeholder(self, monkeypatch, _enabled) -> None:
        monkeypatch.setattr(vs, "capture_screen", lambda: b"fake-png-bytes")
        monkeypatch.setattr(vs.requests, "post", lambda *a, **kw: _FakeResponse(200, ""))
        out = vs.describe_screen("q")
        assert "празен отговор" in out

    def test_non_200_reports_status_code(self, monkeypatch, _enabled) -> None:
        monkeypatch.setattr(vs, "capture_screen", lambda: b"fake-png-bytes")
        monkeypatch.setattr(vs.requests, "post", lambda *a, **kw: _FakeResponse(500))
        out = vs.describe_screen("q")
        assert "HTTP 500" in out

    def test_connection_error_suggests_pulling_the_model(self, monkeypatch, _enabled) -> None:
        monkeypatch.setattr(vs, "capture_screen", lambda: b"fake-png-bytes")

        def _raise(*a, **kw):
            raise vs.requests.exceptions.RequestException("connection refused")
        monkeypatch.setattr(vs.requests, "post", _raise)
        out = vs.describe_screen("q")
        assert "недостъпен през Ollama" in out
        assert "ollama pull" in out

    def test_blank_question_falls_back_to_a_default_prompt(self, monkeypatch, _enabled) -> None:
        monkeypatch.setattr(vs, "capture_screen", lambda: b"fake-png-bytes")
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["prompt"] = json["messages"][0]["content"]
            return _FakeResponse(200, "ok")

        monkeypatch.setattr(vs.requests, "post", fake_post)
        vs.describe_screen("   ")
        assert "Опиши какво виждаш" in captured["prompt"]

    def test_custom_question_is_used_verbatim(self, monkeypatch, _enabled) -> None:
        monkeypatch.setattr(vs, "capture_screen", lambda: b"fake-png-bytes")
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["prompt"] = json["messages"][0]["content"]
            return _FakeResponse(200, "ok")

        monkeypatch.setattr(vs.requests, "post", fake_post)
        vs.describe_screen("how many windows are open?")
        assert captured["prompt"] == "how many windows are open?"
