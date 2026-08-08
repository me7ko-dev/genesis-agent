"""genesis_agent.conversation_memory — persistent chat history + auto-summary,
used by genesis_terminal_agent.py, agent_core.py, and discord_bot.py. Zero
coverage before this file, including of the exact regression its own
docstring describes (2026-07-25): compacting to `threshold` instead of a
buffer under it made get_history() look "frozen" because every add past the
threshold immediately re-triggered a 1-message compression, net growth zero.
That's the primary thing under test here, not just the CRUD operations.

DB_PATH is bound at import time (see tests/conftest.py's module docstring),
so every test redirects the module's own DB_PATH attribute to a tmp file
rather than patching genesis_agent.config.DATA_DIR after the fact.
"""
from __future__ import annotations

import pytest

import genesis_agent.conversation_memory as cm


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "DB_PATH", str(tmp_path / "conversation_memory.db"))


class TestAddAndGetHistory:
    def test_round_trips_in_chronological_order(self) -> None:
        cm.add_message("user", "hi")
        cm.add_message("assistant", "hello")
        cm.add_message("user", "how are you")
        history = cm.get_history()
        assert history == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "how are you"},
        ]

    def test_get_history_respects_last_n(self) -> None:
        for i in range(5):
            cm.add_message("user", f"msg {i}")
        history = cm.get_history(last_n=2)
        assert [h["content"] for h in history] == ["msg 3", "msg 4"]

    def test_empty_db_returns_empty_list(self) -> None:
        assert cm.get_history() == []


class TestSummarizeOldContext:
    def test_below_threshold_does_nothing(self) -> None:
        for i in range(10):
            cm.add_message("user", f"msg {i}")
        cm.summarize_old_context(threshold=50)
        assert len(cm.get_history(last_n=1000)) == 10

    def test_above_threshold_compresses_into_one_summary_message(self) -> None:
        for i in range(12):
            cm.add_message("user", f"msg {i}")
        cm.summarize_old_context(threshold=10, keep=4)
        history = cm.get_history(last_n=1000)
        # 4 kept originals + 1 summary message appended by the compression —
        # the summary is INSERTed after the DELETE, so it gets the newest
        # (highest) id and sorts last, not first.
        assert len(history) == 5
        assert history[-1]["role"] == "system"
        assert "[Context summary]" in history[-1]["content"]
        # The most recent originals must survive untouched, in order.
        assert [h["content"] for h in history[:-1]] == ["msg 8", "msg 9", "msg 10", "msg 11"]

    def test_growth_is_not_erased_by_repeated_compression_near_the_threshold(self) -> None:
        """The exact bug the module's docstring documents: compacting to
        `threshold` (buffer=0) made every add past it re-trigger a 1-message
        compaction, so the count oscillated instead of growing. With the
        default buffer (keep = threshold - 20) it must actually grow."""
        threshold = 30
        for i in range(threshold + 5):
            cm.add_message("user", f"msg {i}")
            cm.summarize_old_context(threshold=threshold)

        count_after_first_batch = len(cm.get_history(last_n=1000))

        for i in range(10):
            cm.add_message("user", f"more {i}")
            cm.summarize_old_context(threshold=threshold)

        count_after_second_batch = len(cm.get_history(last_n=1000))
        assert count_after_second_batch > count_after_first_batch


class TestClearSession:
    def test_clears_all_messages(self) -> None:
        cm.add_message("user", "hi")
        cm.clear_session()
        assert cm.get_history() == []

    def test_usable_again_after_clearing(self) -> None:
        cm.add_message("user", "before")
        cm.clear_session()
        cm.add_message("user", "after")
        assert cm.get_history() == [{"role": "user", "content": "after"}]
