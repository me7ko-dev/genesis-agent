"""genesis_agent.agent_core — the shared tool loop behind every full frontend
(terminal, Discord, GTK, Jarvis), previously untested. Covers the pure
helpers (env_facts, _diff_for_write, _is_question/_clean_question) and
run_tool_loop's control flow with a fake Core/skills bridge — no real Brain
call, no real tool dispatch, no real filesystem writes outside tmp_path."""
from __future__ import annotations

import pytest

from genesis_agent import agent_core as ac


class TestEnvFacts:
    def test_includes_home_directory_and_user(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USER", "testuser")
        out = ac.env_facts()
        assert str(tmp_path) in out
        assert "testuser" in out

    def test_missing_standard_dir_is_flagged(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        out = ac.env_facts()
        assert "(НЕ съществува)" in out

    def test_existing_standard_dir_is_not_flagged_for_that_line(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "Desktop").mkdir()
        out = ac.env_facts()
        desktop_line = next(line for line in out.splitlines() if "Десктоп" in line)
        assert "НЕ съществува" not in desktop_line

    def test_workspace_line_included_only_when_given(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert "Работна директория" not in ac.env_facts()
        assert "Работна директория" in ac.env_facts("/some/workspace")

    def test_xdg_user_dirs_override_the_default(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".config").mkdir()
        custom = tmp_path / "MyDesktopFolder"
        custom.mkdir()
        (tmp_path / ".config" / "user-dirs.dirs").write_text(
            f'XDG_DESKTOP_DIR="{custom}"\n', encoding="utf-8"
        )
        out = ac.env_facts()
        assert str(custom) in out


class _FakeSkills:
    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = files or {}

    def _resolve(self, path: str):
        class _Target:
            def __init__(self, exists: bool, content: str) -> None:
                self._exists = exists
                self._content = content

            def is_file(self) -> bool:
                return self._exists

            def read_text(self, encoding="utf-8", errors="replace") -> str:
                return self._content

        return _Target(path in self.files, self.files.get(path, ""))


class TestDiffForWrite:
    def test_new_file_shows_a_diff_against_empty(self) -> None:
        skills = _FakeSkills()
        diff = ac._diff_for_write(skills, {"path": "new.py", "content": "print(1)\n"})
        assert diff is not None
        assert "+print(1)" in diff

    def test_unchanged_content_returns_none(self) -> None:
        skills = _FakeSkills({"a.py": "same\n"})
        diff = ac._diff_for_write(skills, {"path": "a.py", "content": "same\n"})
        assert diff is None

    def test_changed_content_shows_add_and_remove_lines(self) -> None:
        skills = _FakeSkills({"a.py": "old\n"})
        diff = ac._diff_for_write(skills, {"path": "a.py", "content": "new\n"})
        assert "-old" in diff
        assert "+new" in diff

    def test_missing_path_returns_none(self) -> None:
        assert ac._diff_for_write(_FakeSkills(), {"content": "x"}) is None

    def test_missing_content_returns_none(self) -> None:
        assert ac._diff_for_write(_FakeSkills(), {"path": "a.py"}) is None

    def test_resolve_failure_is_swallowed_not_raised(self) -> None:
        class _Boom:
            def _resolve(self, path):
                raise RuntimeError("path escapes workspace")

        assert ac._diff_for_write(_Boom(), {"path": "a.py", "content": "x"}) is None


class TestQuestionMarkers:
    def test_is_question_true_when_marker_present(self) -> None:
        from genesis_skills import ASK_USER_MARKER
        assert ac._is_question(f"some text {ASK_USER_MARKER} more") is True

    def test_is_question_false_for_plain_text(self) -> None:
        assert ac._is_question("just a normal tool result") is False

    def test_is_question_false_for_empty(self) -> None:
        assert ac._is_question("") is False

    def test_clean_question_strips_the_marker(self) -> None:
        from genesis_skills import ASK_USER_MARKER
        cleaned = ac._clean_question(f"  {ASK_USER_MARKER}Which file?  ")
        assert cleaned == "Which file?"
        assert ASK_USER_MARKER not in cleaned


class _FakeCore:
    def __init__(self, replies) -> None:
        self._replies = list(replies)
        self.skills = None
        self.wm = None
        self.remembered: list[tuple[str, str]] = []

    def complete(self, messages):
        assert self._replies, "core.complete() called more times than the test queued"
        return self._replies.pop(0)

    def remember(self, role, content) -> None:
        self.remembered.append((role, content))


class _FakeToolSkills:
    """Fake genesis_skills bridge for run_tool_loop's native tool_calls path."""

    def __init__(self, dispatch_results: list[str]) -> None:
        self._results = list(dispatch_results)
        self.calls: list[tuple[str, dict]] = []

    def _resolve(self, path):
        raise RuntimeError("not used in these tests")

    def dispatch_tool_call(self, name, args):
        self.calls.append((name, args))
        return self._results.pop(0)

    def parse_and_execute_tools(self, text):
        return []


@pytest.fixture(autouse=True)
def _no_real_compaction(monkeypatch):
    """compact_chat_history is a static method on the real Brain and hits no
    network for small message lists (below threshold), but patch it anyway
    so tests are explicit about what "compaction happened" means."""
    monkeypatch.setattr(
        "genesis_agent.brain.Brain.compact_chat_history",
        staticmethod(lambda messages, threshold=16, keep_recent=10: messages),
    )
    yield


class TestRunToolLoopTextOnly:
    def test_plain_text_reply_ends_the_loop_immediately(self) -> None:
        core = _FakeCore([("hello there", None, "groq", "llama")])
        core.skills = _FakeToolSkills([])
        seen = []
        result = ac.run_tool_loop(
            core, [{"role": "user", "content": "hi"}],
            on_assistant=lambda t, p, m: seen.append((t, p, m)),
            on_tool_result=lambda *a: pytest.fail("no tool should run"),
        )
        assert seen == [("hello there", "groq", "llama")]
        assert result[-1] == {"role": "assistant", "content": "hello there"}


class TestRunToolLoopNativeToolCalls:
    def test_dispatches_a_tool_call_and_continues(self) -> None:
        tool_calls = [{"id": "1", "function": {"name": "RUN_CMD", "arguments": '{"cmd": "ls"}'}}]
        core = _FakeCore([
            ("", tool_calls, "groq", "llama"),
            ("done", None, "groq", "llama"),
        ])
        core.skills = _FakeToolSkills(["file1\nfile2"])
        tool_results = []
        result = ac.run_tool_loop(
            core, [{"role": "user", "content": "list files"}],
            on_assistant=lambda t, p, m: None,
            on_tool_result=lambda name, r, extra: tool_results.append((name, r, extra)),
        )
        assert tool_results == [("RUN_CMD", "file1\nfile2", None)]
        assert core.skills.calls == [("RUN_CMD", {"cmd": "ls"})]
        assert result[-1] == {"role": "assistant", "content": "done"}

    def test_ask_user_stops_the_loop_and_cleans_the_marker(self) -> None:
        from genesis_skills import ASK_USER_MARKER
        tool_calls = [{"id": "1", "function": {"name": "ASK_USER", "arguments": "{}"}}]
        core = _FakeCore([("", tool_calls, "groq", "llama")])
        core.skills = _FakeToolSkills([f"{ASK_USER_MARKER}Which file?"])
        assistant_msgs = []
        ac.run_tool_loop(
            core, [{"role": "user", "content": "do something"}],
            on_assistant=lambda t, p, m: assistant_msgs.append(t),
            on_tool_result=lambda *a: None,
        )
        assert assistant_msgs[-1] == "Which file?"
        # complete() must NOT have been called a second time — asking a
        # question hands control back to the human, it doesn't self-continue.
        assert core._replies == []

    def test_round_cap_stops_an_infinite_tool_loop(self) -> None:
        tool_calls = [{"id": "1", "function": {"name": "RUN_CMD", "arguments": "{}"}}]
        # 3 rounds of tool_calls, cap=2 -> stops after round 2 without needing
        # a 3rd complete() call.
        core = _FakeCore([
            ("", tool_calls, "p", "m"),
            ("", tool_calls, "p", "m"),
        ])
        core.skills = _FakeToolSkills(["ok", "ok"])
        assistant_msgs = []
        ac.run_tool_loop(
            core, [{"role": "user", "content": "loop forever"}],
            on_assistant=lambda t, p, m: assistant_msgs.append(t),
            on_tool_result=lambda *a: None,
            round_cap=2,
        )
        assert "таван" in assistant_msgs[-1]


class TestRunToolLoopTextTagFallback:
    def test_text_tag_tools_are_parsed_and_executed(self) -> None:
        core = _FakeCore([
            ("[RUN_CMD: ls]", None, "p", "m"),
            ("all done", None, "p", "m"),
        ])

        class _TextTagSkills(_FakeToolSkills):
            def parse_and_execute_tools(self, text):
                return ["file1\nfile2"] if "[RUN_CMD" in text else []

        core.skills = _TextTagSkills([])
        tool_results = []
        result = ac.run_tool_loop(
            core, [{"role": "user", "content": "list files"}],
            on_assistant=lambda t, p, m: None,
            on_tool_result=lambda name, r, extra: tool_results.append((name, r)),
        )
        assert tool_results == [("инструмент", "file1\nfile2")]
        assert result[-1] == {"role": "assistant", "content": "all done"}

    def test_no_parseable_tools_ends_the_loop(self) -> None:
        core = _FakeCore([("just plain text, no tags", None, "p", "m")])
        core.skills = _FakeToolSkills([])
        result = ac.run_tool_loop(
            core, [{"role": "user", "content": "hi"}],
            on_assistant=lambda t, p, m: None,
            on_tool_result=lambda *a: pytest.fail("no tool should run"),
        )
        assert result[-1] == {"role": "assistant", "content": "just plain text, no tags"}


class TestRunToolLoopCompaction:
    def test_shrinking_history_triggers_auto_capture_and_status(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "genesis_agent.brain.Brain.compact_chat_history",
            staticmethod(lambda messages, threshold=16, keep_recent=10: messages[-1:]),
        )
        core = _FakeCore([("hi", None, "p", "m")])
        core.skills = _FakeToolSkills([])
        captured = []

        class _WM:
            def auto_capture(self, pre_compact):
                captured.append(pre_compact)

        core.wm = _WM()
        statuses = []
        ac.run_tool_loop(
            core, [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            on_assistant=lambda t, p, m: None,
            on_tool_result=lambda *a: None,
            on_status=lambda s: statuses.append(s),
        )
        assert captured, "auto_capture should have been called once history shrank"
        assert any("компресирана" in s for s in statuses)

    def test_auto_capture_failure_does_not_propagate(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "genesis_agent.brain.Brain.compact_chat_history",
            staticmethod(lambda messages, threshold=16, keep_recent=10: messages[-1:]),
        )
        core = _FakeCore([("hi", None, "p", "m")])
        core.skills = _FakeToolSkills([])

        class _BoomWM:
            def auto_capture(self, pre_compact):
                raise RuntimeError("disk full")

        core.wm = _BoomWM()
        ac.run_tool_loop(
            core, [{"role": "user", "content": "hi"}],
            on_assistant=lambda t, p, m: None,
            on_tool_result=lambda *a: None,
        )


class TestLooksLikeEnglish:
    """Pure-function heuristic (design note, 2026-08-11): no LLM call, just
    decides whether translation is even worth attempting."""

    def test_cyrillic_text_is_not_english(self) -> None:
        assert ac._looks_like_english("Напиши функция за проверка дали число е просто") is False

    def test_short_latin_snippet_is_not_worth_translating(self) -> None:
        assert ac._looks_like_english("OK") is False
        assert ac._looks_like_english("done") is False

    def test_long_latin_prose_is_english(self) -> None:
        assert ac._looks_like_english(
            "This is a longer English sentence explaining what the code does."
        ) is True

    def test_code_fence_does_not_count_toward_the_latin_threshold(self) -> None:
        # All the Latin letters are inside the fence; outside it there's
        # Cyrillic -- must NOT be flagged as English.
        text = "Ето кода:\n```python\ndef merge_intervals(intervals): pass\n```"
        assert ac._looks_like_english(text) is False

    def test_empty_text_is_not_english(self) -> None:
        assert ac._looks_like_english("") is False


class TestToUserText:
    def test_bulgarian_text_passes_through_untranslated(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "genesis_agent.translator.translate_en_to_bg",
            lambda t: pytest.fail("should not be called for Bulgarian text"),
        )
        assert ac._to_user_text("Готово е.") == "Готово е."

    def test_long_english_text_gets_translated(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "genesis_agent.translator.translate_en_to_bg",
            lambda t: "ПРЕВЕДЕНО: " + t,
        )
        out = ac._to_user_text("This is a longer English sentence to translate.")
        assert out.startswith("ПРЕВЕДЕНО: ")

    def test_translator_failure_falls_back_to_the_original_text(self, monkeypatch) -> None:
        def _boom(t):
            raise RuntimeError("ollama unreachable")

        monkeypatch.setattr("genesis_agent.translator.translate_en_to_bg", _boom)
        original = "This is a longer English sentence that fails to translate."
        assert ac._to_user_text(original) == original


class TestTranslateLastUserMessageToEn:
    def test_bulgarian_user_message_is_translated_in_place(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "genesis_agent.translator.translate_bg_to_en",
            lambda t: "translated: " + t,
        )
        messages = [{"role": "system", "content": "sys"},
                    {"role": "user", "content": "Напиши функция"}]
        ac._translate_last_user_message_to_en(messages)
        assert messages[-1]["content"] == "translated: Напиши функция"
        assert messages[0]["content"] == "sys"  # untouched

    def test_already_english_user_message_is_left_alone(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "genesis_agent.translator.translate_bg_to_en",
            lambda t: pytest.fail("should not be called for English text"),
        )
        messages = [{"role": "user", "content": "Write a function"}]
        ac._translate_last_user_message_to_en(messages)
        assert messages[-1]["content"] == "Write a function"

    def test_non_user_last_message_is_left_alone(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "genesis_agent.translator.translate_bg_to_en",
            lambda t: pytest.fail("should not be called when the last message isn't from the user"),
        )
        messages = [{"role": "user", "content": "Напиши функция"},
                    {"role": "assistant", "content": "Готово"}]
        ac._translate_last_user_message_to_en(messages)
        assert messages[-1]["content"] == "Готово"

    def test_translator_failure_leaves_the_original_bulgarian_text(self, monkeypatch) -> None:
        def _boom(t):
            raise RuntimeError("ollama unreachable")

        monkeypatch.setattr("genesis_agent.translator.translate_bg_to_en", _boom)
        messages = [{"role": "user", "content": "Напиши функция"}]
        ac._translate_last_user_message_to_en(messages)
        assert messages[-1]["content"] == "Напиши функция"


class TestRunToolLoopBilingualRoundTrip:
    """End-to-end: a Bulgarian user message reaches the model in English, and
    a long English model reply reaches on_assistant in Bulgarian -- the two
    halves of the BG<->EN sandwich (design note, 2026-08-11), wired into the
    shared loop every frontend calls."""

    def test_user_bg_in_model_en_out_bg_to_the_user(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "genesis_agent.translator.translate_bg_to_en",
            lambda t: "EN: " + t,
        )
        monkeypatch.setattr(
            "genesis_agent.translator.translate_en_to_bg",
            lambda t: "BG: " + t,
        )
        long_english_reply = "This is a sufficiently long English explanation of the fix."
        core = _FakeCore([(long_english_reply, None, "groq", "llama")])
        core.skills = _FakeToolSkills([])
        seen = []
        messages = [{"role": "user", "content": "Обясни ми поправката"}]

        ac.run_tool_loop(
            core, messages,
            on_assistant=lambda t, p, m: seen.append(t),
            on_tool_result=lambda *a: pytest.fail("no tool should run"),
        )

        # The model saw the ENGLISH translation, not the raw Bulgarian.
        assert messages[0]["content"] == "EN: Обясни ми поправката"
        # The user saw the BULGARIAN translation of the model's English reply.
        assert seen == ["BG: " + long_english_reply]
