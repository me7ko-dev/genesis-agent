"""genesis_terminal_agent — пътят до модела и всичко около хода.

Директните HTTP извиквания (резервният път за доставчици, които Brain не
знае), изборът кой път да се ползва, екранът на хода (RichTurnUI), системният
промпт и потвърждението на sandbox-а. Всичко е без мрежа: requests и Brain
са подменени.
"""
from __future__ import annotations

import io
from collections import deque
from types import SimpleNamespace
from typing import ClassVar

import pytest
from rich.console import Console

import genesis_terminal_agent as gta


class _Resp:
    def __init__(self, status: int, body=None, text: str = "") -> None:
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def screen(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(gta, "console", Console(file=buf, width=200, color_system=None))
    return buf


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(gta, "MODELS_CACHE", {})


# ── Списъкът с модели ────────────────────────────────────────────────────────

class TestFetchModels:
    def test_an_openai_style_list_is_sorted_and_cached(self, monkeypatch) -> None:
        calls = []
        monkeypatch.setitem(gta.KEYS, "NVIDIA_API_KEY", "nv")

        def _get(url, headers=None, timeout=None):
            calls.append((url, headers))
            return _Resp(200, {"data": [{"id": "b"}, {"id": "a"}]})
        monkeypatch.setattr(gta.requests, "get", _get)
        assert gta.fetch_models("nvidia") == ["a", "b"]
        assert gta.fetch_models("nvidia") == ["a", "b"]
        assert len(calls) == 1, "вторият път е от кеша"
        assert calls[0] == ("https://integrate.api.nvidia.com/v1/models", {"Authorization": "Bearer nv"})

    def test_a_failed_list_falls_back_to_the_known_models(self, monkeypatch) -> None:
        def _get(*a, **kw):
            raise ConnectionError("няма мрежа")
        monkeypatch.setattr(gta.requests, "get", _get)
        assert gta.fetch_models("groq") == gta.FALLBACKS["groq"]

    def test_local_ollama_lists_what_is_pulled(self, monkeypatch) -> None:
        monkeypatch.setattr(gta.requests, "get",
                            lambda url, timeout=None: _Resp(200, {"models": [{"name": "qwen3:14b"}]}))
        assert gta.fetch_models("ollama") == ["qwen3:14b"]

    def test_local_ollama_without_models_says_so(self, monkeypatch) -> None:
        monkeypatch.setattr(gta.requests, "get", lambda url, timeout=None: _Resp(200, {"models": []}))
        assert gta.fetch_models("ollama") == ["__no_models__"]

    def test_vertex_with_a_token_lists_live_models(self, monkeypatch) -> None:
        from genesis_agent import vertex_auth
        monkeypatch.setattr(vertex_auth, "projects", lambda: ["проект"])
        monkeypatch.setattr(vertex_auth, "token", lambda p: "tok")
        monkeypatch.setattr(vertex_auth, "endpoint", lambda p: "https://vertex.example/v1")
        monkeypatch.setattr(gta.requests, "get", lambda url, headers=None, timeout=None: _Resp(
            200, {"data": [{"id": "google/gemini-2.5-pro"}]}))
        assert gta.fetch_models("vertex") == ["google/gemini-2.5-pro"]


# ── Директните извиквания ────────────────────────────────────────────────────

class TestOpenAICompatible:
    def test_openrouter_gets_its_headers_and_tools_go_in_the_payload(self, monkeypatch) -> None:
        seen: dict = {}
        monkeypatch.setitem(gta.KEYS, "OPENROUTER_API_KEY", "or")

        def _post(url, json=None, headers=None, timeout=None):
            seen.update(url=url, payload=json, headers=headers)
            return _Resp(200, {"choices": [{"message": {"content": " ок ", "tool_calls": [{"id": "1"}]},
                                            "finish_reason": "tool_calls"}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
        monkeypatch.setattr(gta.requests, "post", _post)
        content, calls = gta.call_openai_compatible([{"role": "user", "content": "hi"}],
                                                    "openrouter", "m", tools=[{"type": "function"}])
        assert (content, calls) == ("ок", [{"id": "1"}])
        assert seen["headers"]["X-Title"] == "Genesis Agent"
        assert seen["payload"]["tools"] == [{"type": "function"}]
        assert seen["payload"]["tool_choice"] == "auto"
        assert gta._last_usage == {"prompt_tokens": 10, "completion_tokens": 2}

    def test_an_error_body_is_quoted(self, monkeypatch) -> None:
        monkeypatch.setattr(gta.requests, "post", lambda *a, **kw: _Resp(
            429, {"error": {"message": "rate limit"}}))
        with pytest.raises(RuntimeError, match="HTTP_429: rate limit"):
            gta.call_openai_compatible([], "groq", "m")

    def test_an_error_that_is_not_json(self, monkeypatch) -> None:
        monkeypatch.setattr(gta.requests, "post", lambda *a, **kw: _Resp(
            502, ValueError("not json"), text="Bad Gateway"))
        with pytest.raises(RuntimeError, match="HTTP_502: Bad Gateway"):
            gta.call_openai_compatible([], "groq", "m")

    def test_a_connection_error(self, monkeypatch) -> None:
        def _post(*a, **kw):
            raise OSError("refused")
        monkeypatch.setattr(gta.requests, "post", _post)
        with pytest.raises(RuntimeError, match="CONN: refused"):
            gta.call_openai_compatible([], "groq", "m")


class TestGeminiAndOllama:
    def test_gemini_gets_the_system_prompt_separately(self, monkeypatch) -> None:
        seen = {}

        def _post(url, json=None, timeout=None, headers=None):
            seen["payload"] = json
            return _Resp(200, {"candidates": [{"content": {"parts": [{"text": " да "}]}}],
                               "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 1}})
        monkeypatch.setattr(gta.requests, "post", _post)
        out = gta.call_gemini([{"role": "system", "content": "бъди кратък"},
                               {"role": "user", "content": "може ли?"},
                               {"role": "assistant", "content": "да"}], "gemini-2.5-flash")
        assert out == "да"
        assert seen["payload"]["systemInstruction"] == {"parts": [{"text": "бъди кратък\n"}]}
        assert [c["role"] for c in seen["payload"]["contents"]] == ["user", "model"]
        assert gta._last_usage == {"prompt_tokens": 7, "completion_tokens": 1}

    def test_gemini_errors_come_back_as_text(self, monkeypatch) -> None:
        monkeypatch.setattr(gta.requests, "post", lambda *a, **kw: _Resp(200, {"candidates": []}))
        assert gta.call_gemini([], "m") == "[Грешка: Празен отговор]"
        monkeypatch.setattr(gta.requests, "post", lambda *a, **kw: _Resp(403, {}))
        assert gta.call_gemini([], "m") == "[Грешка 403]"

    def test_ollama_answers_with_its_usage(self, monkeypatch) -> None:
        monkeypatch.setattr(gta.requests, "post", lambda *a, **kw: _Resp(
            200, {"message": {"content": " здрасти "}, "prompt_eval_count": 5, "eval_count": 3}))
        assert gta.call_ollama([], "llama3.2") == "здрасти"
        assert gta._last_usage == {"prompt_tokens": 5, "completion_tokens": 3}

    def test_ollama_errors_come_back_as_text(self, monkeypatch) -> None:
        monkeypatch.setattr(gta.requests, "post", lambda *a, **kw: _Resp(500, {}, text="зареждане"))
        assert gta.call_ollama([], "m") == "[Ollama грешка 500: зареждане]"

        def _post(*a, **kw):
            raise OSError("не слуша")
        monkeypatch.setattr(gta.requests, "post", _post)
        assert gta.call_ollama([], "m") == "[Ollama не отговаря: не слуша]"


class TestRouting:
    def test_each_type_goes_to_its_function(self, monkeypatch) -> None:
        monkeypatch.setattr(gta, "call_gemini", lambda m, model: "gemini")
        monkeypatch.setattr(gta, "call_ollama", lambda m, model: "ollama")
        monkeypatch.setattr(gta, "call_openai_compatible",
                            lambda m, prov, model, tools=None: (f"openai:{prov}", tools))
        assert gta._call_provider("gemini", "x", []) == ("gemini", None)
        assert gta._call_provider("ollama", "x", []) == ("ollama", None)
        assert gta._call_provider("github", "x", [], tools=["t"]) == ("openai:github", ["t"])
        assert gta._call_provider("непознат", "x", []) == ("openai:непознат", None)

    def test_tool_history_becomes_text_for_a_model_without_tools(self) -> None:
        out = gta._sanitize_for_textmode([
            {"role": "user", "content": "списък"},
            {"role": "assistant", "content": "гледам",
             "tool_calls": [{"function": {"name": "LIST_DIR", "arguments": '{"path": "."}'}}]},
            {"role": "tool", "content": "a.py"},
        ])
        assert out[0] == {"role": "user", "content": "списък"}
        assert out[1] == {"role": "assistant",
                          "content": 'гледам\n[извикани tool-ове: LIST_DIR({"path": "."})]'}
        assert out[2] == {"role": "user", "content": "[Резултат от tool]: a.py"}


class TestTheLegacyPath:
    def test_tools_only_for_a_model_that_supports_them(self, monkeypatch) -> None:
        seen = []

        def _call(prov, model, msgs, tools=None):
            seen.append((msgs, tools))
            return "отговор", None
        monkeypatch.setattr(gta, "_call_provider", _call)
        monkeypatch.setattr(gta, "_SUPPORTS_TOOLS", {("github", "gpt-4o"): True})
        history = [{"role": "tool", "content": "x"}]
        gta._ask_via_legacy(history, ["t"], "github", "gpt-4o")
        gta._ask_via_legacy(history, ["t"], "github", "друг")
        assert seen[0] == (history, ["t"])
        assert seen[1] == ([{"role": "user", "content": "[Резултат от tool]: x"}], None)

    def test_a_failure_comes_back_as_text(self, monkeypatch) -> None:
        def _fail(*a, **kw):
            raise RuntimeError("HTTP_500: down")
        monkeypatch.setattr(gta, "_call_provider", _fail)
        assert gta._ask_via_legacy([], None, "github", "m") == ("[Грешка: HTTP_500: down]", None)

    def test_real_usage_is_recorded(self, monkeypatch) -> None:
        recorded = []

        def _call(prov, model, msgs, tools=None):
            gta._last_usage = {"prompt_tokens": 100, "completion_tokens": 9}
            return "ок", None
        monkeypatch.setattr(gta, "_call_provider", _call)
        monkeypatch.setattr("genesis_agent.budget.record_usage", lambda **kw: recorded.append(kw))
        gta.reset_usage()
        gta._ask_via_legacy([], None, "github", "m")
        assert recorded == [{"provider": "github", "model": "m",
                             "prompt_tokens": 100, "completion_tokens": 9}]
        assert gta.total_input_tokens == 100
        gta.reset_usage()


class _Brain:
    reply = SimpleNamespace(raw_text="силен", tool_calls=None, usage=None)
    answered_by: ClassVar[dict[str, str]] = {"provider": "groq", "model": "openai/gpt-oss-120b"}

    def __init__(self, *a, **kw) -> None:
        self.current = dict(_Brain.answered_by)

    def complete(self, messages, tools=None):
        return _Brain.reply


class TestAskGenesis:
    @pytest.fixture(autouse=True)
    def _strong(self, monkeypatch):
        monkeypatch.setattr("genesis_agent.brain.Brain", _Brain)
        monkeypatch.setattr(gta, "_CODING_MODE", True)  # без лекия маршрут
        monkeypatch.setattr(gta, "current_provider", "groq")
        monkeypatch.setattr(gta, "current_model_id", "openai/gpt-oss-120b")
        _Brain.reply = SimpleNamespace(raw_text="силен", tool_calls=None, usage=None)
        _Brain.answered_by = {"provider": "groq", "model": "openai/gpt-oss-120b"}

    def test_a_provider_brain_does_not_know_takes_the_legacy_path(self, monkeypatch) -> None:
        monkeypatch.setattr(gta, "current_provider", "github")
        monkeypatch.setattr(gta, "_ask_via_legacy", lambda m, t, p, mod: (f"legacy:{p}", None))
        assert gta.ask_genesis([], tools=None) == ("legacy:github", None)

    def test_an_answer_from_another_model_is_shown_but_the_pin_stays(self, monkeypatch, screen) -> None:
        _Brain.answered_by = {"provider": "nvidia", "model": "резервен"}
        assert gta.ask_genesis([{"role": "user", "content": "x"}]) == ("силен", None)
        assert "Отговорено от → nvidia / резервен" in screen.getvalue()
        assert gta.current_provider == "groq"

    def test_a_chain_error_is_marked_as_an_error(self) -> None:
        _Brain.reply = SimpleNamespace(raw_text="Error: всички паднаха", tool_calls=None, usage=None)
        assert gta.ask_genesis([]) == ("[Грешка: всички паднаха]", None)


# ── Екранът на хода, системният промпт, потвърждението ───────────────────────

class TestRichTurnUI:
    def test_every_event_is_drawn(self, screen) -> None:
        ui = gta.RichTurnUI()
        with ui.thinking("мисля"):
            pass
        ui.assistant("**готово**")
        ui.assistant("   ")
        ui.tool("RUN_CMD", "x" * 3000)
        ui.asked("кой порт?")
        ui.spinning("въртене")
        ui.warn("внимание")
        ui.info("бележка")
        out = screen.getvalue()
        for text in ("готово", "🔧 RUN_CMD", "Genesis пита", "кой порт?", "Въртене на място",
                     "⚠ внимание", "бележка"):
            assert text in out
        assert "x" * 2001 not in out, "резултатът се реже на 2000 знака"
        assert ui.cancelled() is False


class TestSystemPrompt:
    def test_it_has_the_machine_the_work_and_the_recent_activity(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.workspace_memory.briefing", lambda: "нишка #7")
        monkeypatch.setattr(gta, "_memory_context", lambda n=6: "епизод: тест")
        prompt, briefing = gta.build_system_prompt()
        assert briefing == "нишка #7"
        assert "## СРЕДАТА (реални пътища" in prompt
        assert "## СЪСТОЯНИЕ НА РАБОТАТА (от предишни сесии)\nнишка #7" in prompt
        assert "Скорошна активност" in prompt and "епизод: тест" in prompt

    def test_broken_parts_are_left_out_not_fatal(self, monkeypatch) -> None:
        def _boom(*a, **kw):
            raise RuntimeError("нарочно")
        monkeypatch.setattr("genesis_agent.workspace_memory.briefing", _boom)
        monkeypatch.setattr("genesis_agent.agent_core.env_facts", _boom)
        monkeypatch.setattr(gta, "_memory_context", _boom)
        prompt, briefing = gta.build_system_prompt()
        assert briefing == "" and prompt
        assert "## СРЕДАТА (реални пътища" not in prompt
        assert "## СЪСТОЯНИЕ НА РАБОТАТА (от предишни сесии)" not in prompt

    def test_an_empty_activity_log_is_not_injected(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.workspace_memory.briefing", lambda: "")
        monkeypatch.setattr(gta, "_memory_context", lambda n=6: "Няма записани епизоди.")
        prompt, _ = gta.build_system_prompt()
        assert "Скорошна активност" not in prompt


class TestSandboxConfirmation:
    @pytest.mark.parametrize(("answer", "allowed"), [("y", True), ("да", True), ("", False), ("не", False)])
    def test_only_an_explicit_yes_runs_it(self, monkeypatch, screen, answer, allowed) -> None:
        monkeypatch.setattr(gta.console, "input", lambda prompt="": answer)
        verdict = SimpleNamespace(reasons=["трие файлове"])
        assert gta._terminal_confirm("rm -rf build", verdict) is allowed
        assert "трие файлове" in screen.getvalue()

    def test_eof_or_ctrl_c_is_a_no(self, monkeypatch, screen) -> None:
        def _eof(prompt=""):
            raise EOFError
        monkeypatch.setattr(gta.console, "input", _eof)
        assert gta._terminal_confirm("rm x", SimpleNamespace(reasons=[])) is False


class TestSmallHelpers:
    def test_elapsed_time_reads_naturally(self, monkeypatch) -> None:
        for seconds, text in ((5, "5s"), (125, "2m 5s"), (3725, "1h 2m")):
            monkeypatch.setattr(gta.time, "time", lambda s=seconds: gta.session_start_time + s)
            assert gta.get_elapsed_time() == text

    def test_a_long_model_name_is_shortened_in_the_status_bar(self, monkeypatch) -> None:
        monkeypatch.setattr(gta, "current_model_id", "vendor/a-very-long-model-name-indeed")
        assert "a-very-long-model..." in gta.build_status_bar().plain

    def test_remember_never_raises(self, monkeypatch) -> None:
        def _boom(role, content):
            raise RuntimeError("заключена база")
        monkeypatch.setattr(gta, "_conv_mem", SimpleNamespace(add_message=_boom))
        gta._remember("user", "здрасти")
        stored = []
        monkeypatch.setattr(gta, "_conv_mem", SimpleNamespace(add_message=lambda r, c: stored.append((r, c))))
        gta._remember("user", "")
        gta._remember("user", "здрасти")
        assert stored == [("user", "здрасти")]

    def test_the_restored_session_keeps_the_new_prompt(self) -> None:
        restored = gta._restore_session([{"role": "system", "content": "стар"},
                                         {"role": "user", "content": "въпрос"}], "нов")
        assert isinstance(restored, deque)
        assert list(restored) == [{"role": "system", "content": "нов"},
                                  {"role": "user", "content": "въпрос"}]
