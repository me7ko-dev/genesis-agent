"""genesis_agent.brain — pure-logic pieces that don't need a live provider:
trim_round_history (token-savings for multi-round missions), _provider_key
(one-key-per-provider lookup), and _http's response handling (the exact
KeyError-on-200-with-no-choices bug found live in the OpenRouter forge test,
2026-07-25 — see the docstring in brain.py)."""
from __future__ import annotations

import pytest

import genesis_agent.brain as brain_mod
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


@pytest.fixture(autouse=True)
def _clean_exhausted_state():
    """_EXHAUSTED is module-level global state, keyed by provider/key id —
    a previous test marking something exhausted must not leak into the next
    one's cooldown assertions."""
    brain_mod._EXHAUSTED.clear()
    yield
    brain_mod._EXHAUSTED.clear()


class TestExhaustionCooldown:
    def test_unmarked_key_is_not_exhausted(self) -> None:
        assert brain_mod._is_exhausted("nobody-marked-this") is False

    def test_marked_key_is_exhausted_immediately(self) -> None:
        brain_mod._mark_exhausted("groq")
        assert brain_mod._is_exhausted("groq") is True

    def test_expired_cooldown_is_no_longer_exhausted_and_self_clears(self, monkeypatch) -> None:
        t = [1_000_000.0]
        monkeypatch.setattr(brain_mod.time, "time", lambda: t[0])
        brain_mod._mark_exhausted("groq")
        assert brain_mod._is_exhausted("groq") is True
        t[0] += brain_mod._EXHAUST_COOLDOWN + 1
        assert brain_mod._is_exhausted("groq") is False
        # Self-clearing: the entry should be gone, not just reported as expired.
        assert "groq" not in brain_mod._EXHAUSTED


class TestLastModelPersistence:
    def _isolate(self, monkeypatch, tmp_path):
        p = tmp_path / "last_model.json"
        monkeypatch.setattr(brain_mod, "last_model_path", lambda: p)
        return p

    def test_missing_file_returns_none(self, monkeypatch, tmp_path) -> None:
        self._isolate(monkeypatch, tmp_path)
        assert brain_mod._load_last_model() is None

    def test_corrupt_file_returns_none_not_an_exception(self, monkeypatch, tmp_path) -> None:
        p = self._isolate(monkeypatch, tmp_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json at all", encoding="utf-8")
        assert brain_mod._load_last_model() is None

    def test_save_then_load_round_trips(self, monkeypatch, tmp_path) -> None:
        self._isolate(monkeypatch, tmp_path)
        brain_mod._save_last_model("groq", "llama-3.3-70b-versatile")
        assert brain_mod._load_last_model() == ("groq", "llama-3.3-70b-versatile")

    def test_save_never_raises_even_if_the_path_is_unwritable(self, monkeypatch, tmp_path) -> None:
        # Point at a path whose parent cannot be created (a file, not a dir).
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setattr(brain_mod, "last_model_path", lambda: blocker / "last_model.json")
        brain_mod._save_last_model("groq", "some-model")  # must not raise


class TestOllamaCloudKeys:
    def _brain_with_keys(self, keys: dict) -> Brain:
        b = Brain.__new__(Brain)
        b.keys = keys
        return b

    def test_empty_when_no_keys_set(self) -> None:
        b = self._brain_with_keys({})
        assert b._ollama_cloud_keys() == []

    def test_single_bare_key_returns_one_item_list(self) -> None:
        b = self._brain_with_keys({"OLLAMA_API_KEY": "k1"})
        assert b._ollama_cloud_keys() == ["k1"]

    def test_numbered_keys_collected_in_order(self) -> None:
        b = self._brain_with_keys({
            "OLLAMA_API_KEY": "k1",
            "OLLAMA_API_KEY_3": "k3",
            "OLLAMA_API_KEY_2": "k2",
        })
        # Order follows _OLLAMA_CLOUD_KEY_ENVS (bare, then _2, _3, ...), not
        # insertion order into the dict.
        assert b._ollama_cloud_keys() == ["k1", "k2", "k3"]

    def test_blank_numbered_key_is_skipped(self) -> None:
        b = self._brain_with_keys({"OLLAMA_API_KEY": "k1", "OLLAMA_API_KEY_2": "   "})
        assert b._ollama_cloud_keys() == ["k1"]


class TestOllamaCloudRotation:
    """_call_ollama_cloud_rotating — the opt-in multi-key path. Only reached
    with >1 key (see brain.py's `_call`), so these call it directly."""

    def _brain(self) -> Brain:
        b = Brain.__new__(Brain)
        b.timeout = 30
        return b

    def test_first_key_succeeds_no_rotation_needed(self, monkeypatch) -> None:
        b = self._brain()
        calls = []

        def fake_http(base_url, key, model, messages, timeout, tools=None):
            calls.append(key)
            return "ok", None

        monkeypatch.setattr(b, "_http", fake_http)
        out = b._call_ollama_cloud_rotating("https://x", "m", [], None, ["k1", "k2"])
        assert out == ("ok", None)
        assert calls == ["k1"]

    def test_exhausted_first_key_rotates_to_second(self, monkeypatch) -> None:
        b = self._brain()
        calls = []

        def fake_http(base_url, key, model, messages, timeout, tools=None):
            calls.append(key)
            if key == "k1":
                raise RuntimeError("HTTP_429: rate limited")
            return "from k2", None

        monkeypatch.setattr(b, "_http", fake_http)
        out = b._call_ollama_cloud_rotating("https://x", "m", [], None, ["k1", "k2"])
        assert out == ("from k2", None)
        assert calls == ["k1", "k2"]
        assert brain_mod._is_exhausted("key::OLLAMA_API_KEY#1") is True
        assert brain_mod._is_exhausted("key::OLLAMA_API_KEY#2") is False

    def test_already_cooling_key_is_skipped_without_being_tried(self, monkeypatch) -> None:
        b = self._brain()
        brain_mod._mark_exhausted("key::OLLAMA_API_KEY#1")
        calls = []

        def fake_http(base_url, key, model, messages, timeout, tools=None):
            calls.append(key)
            return "from k2", None

        monkeypatch.setattr(b, "_http", fake_http)
        out = b._call_ollama_cloud_rotating("https://x", "m", [], None, ["k1", "k2"])
        assert out == ("from k2", None)
        assert calls == ["k2"]

    def test_all_keys_exhausted_raises_a_clear_error(self) -> None:
        b = self._brain()
        brain_mod._mark_exhausted("key::OLLAMA_API_KEY#1")
        brain_mod._mark_exhausted("key::OLLAMA_API_KEY#2")
        with pytest.raises(RuntimeError, match="cooling down"):
            b._call_ollama_cloud_rotating("https://x", "m", [], None, ["k1", "k2"])

    def test_non_fallback_error_propagates_immediately_without_trying_next_key(self, monkeypatch) -> None:
        """A bug (e.g. TypeError) must not be swallowed and silently retried
        as if it were an exhausted-quota response."""
        b = self._brain()
        calls = []

        def fake_http(base_url, key, model, messages, timeout, tools=None):
            calls.append(key)
            raise RuntimeError("HTTP_400: bad request")

        monkeypatch.setattr(b, "_http", fake_http)
        with pytest.raises(RuntimeError, match="HTTP_400"):
            b._call_ollama_cloud_rotating("https://x", "m", [], None, ["k1", "k2"])
        assert calls == ["k1"]  # never tried k2 — this wasn't a quota/auth failure
