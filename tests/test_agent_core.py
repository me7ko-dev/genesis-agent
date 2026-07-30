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
