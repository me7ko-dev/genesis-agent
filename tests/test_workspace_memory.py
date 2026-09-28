"""genesis_agent.workspace_memory — the "what am I actually working on" memory
(threads/decisions/preferences) injected into every frontend's system prompt
via briefing(), previously untested. DB_PATH is redirected to tmp_path for
every test (module-level constant bound at import time, same convention as
gui_sessions.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from genesis_agent import workspace_memory as wm


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(wm, "DB_PATH", tmp_path / "workspace_memory.db")
    monkeypatch.setattr(wm, "_workspace", wm._norm(tmp_path / "project-a"))
    yield


class TestAddThread:
    def test_adds_a_new_thread(self) -> None:
        msg = wm.add_thread("build a CLI", next_step="write the parser")
        assert "✓ Нишка #1" in msg
        [row] = wm.list_threads("open")
        assert row["title"] == "build a CLI"
        assert row["next_step"] == "write the parser"
        assert row["status"] == "open"

    def test_empty_title_is_rejected_without_inserting(self) -> None:
        msg = wm.add_thread("   ")
        assert "Празно заглавие" in msg
        assert wm.list_threads("all") == []

    def test_duplicate_title_case_insensitive_updates_instead_of_duplicating(self) -> None:
        wm.add_thread("Build A CLI", next_step="step one")
        msg = wm.add_thread("build a cli", next_step="step two")
        assert "вече съществува" in msg
        rows = wm.list_threads("all")
        assert len(rows) == 1
        assert rows[0]["next_step"] == "step two"

    def test_title_is_truncated(self) -> None:
        wm.add_thread("x" * 500)
        [row] = wm.list_threads("all")
        assert len(row["title"]) == 300


class TestUpdateThread:
    def test_invalid_id_is_rejected(self) -> None:
        assert "Невалиден id" in wm.update_thread("not-a-number", status="done")

    def test_invalid_status_is_rejected(self) -> None:
        tid = wm.add_thread("a task").split("#")[1].split(":")[0]
        msg = wm.update_thread(tid, status="bogus")
        assert "Невалиден статус" in msg

    def test_no_fields_given_reports_nothing_to_update(self) -> None:
        tid = wm.add_thread("a task").split("#")[1].split(":")[0]
        assert "Нищо за обновяване" in wm.update_thread(tid)

    def test_unknown_id_reports_no_such_thread(self) -> None:
        assert "Няма нишка" in wm.update_thread(999, status="done")

    def test_updates_only_the_given_fields(self) -> None:
        wm.add_thread("a task", next_step="old step", notes="old notes")
        tid = wm.list_threads("open")[0]["id"]
        wm.update_thread(tid, status="blocked")
        row = wm.list_threads("blocked")[0]
        assert row["status"] == "blocked"
        assert row["next_step"] == "old step"  # untouched
        assert row["notes"] == "old notes"      # untouched

    def test_id_with_hash_prefix_is_accepted(self) -> None:
        wm.add_thread("a task")
        tid = wm.list_threads("open")[0]["id"]
        assert "обновена" in wm.update_thread(f"#{tid}", status="done")


class TestListThreads:
    def test_filters_by_status(self) -> None:
        wm.add_thread("open one")
        tid = wm.add_thread("blocked one").split("#")[1].split(":")[0]
        wm.update_thread(tid, status="blocked")
        assert [t["title"] for t in wm.list_threads("open")] == ["open one"]
        assert [t["title"] for t in wm.list_threads("blocked")] == ["blocked one"]

    def test_all_returns_every_status(self) -> None:
        wm.add_thread("one")
        tid = wm.add_thread("two").split("#")[1].split(":")[0]
        wm.update_thread(tid, status="done")
        assert len(wm.list_threads("all")) == 2

    def test_respects_limit(self) -> None:
        for i in range(5):
            wm.add_thread(f"task {i}")
        assert len(wm.list_threads("open", limit=2)) == 2


class TestIsSemanticDup:
    def test_exact_normalized_match_found(self) -> None:
        assert wm._is_semantic_dup("Ползвай tabs!", ["ползвай tabs"]) == "ползвай tabs"

    def test_different_text_returns_none(self) -> None:
        assert wm._is_semantic_dup("use tabs", ["use spaces"]) is None

    def test_empty_existing_list_returns_none(self) -> None:
        assert wm._is_semantic_dup("anything", []) is None


class TestAddDecision:
    def test_adds_a_decision(self) -> None:
        msg = wm.add_decision("use SQLite", why="simple and file-based")
        assert "✓ Записано решение" in msg
        [row] = wm.list_decisions()
        assert row["what"] == "use SQLite"
        assert row["why"] == "simple and file-based"

    def test_empty_what_is_rejected(self) -> None:
        assert "Празно решение" in wm.add_decision("")

    def test_near_duplicate_is_skipped(self) -> None:
        wm.add_decision("Use tabs, not spaces")
        msg = wm.add_decision("use tabs, not spaces!!!")
        assert "Вече е записано" in msg
        assert len(wm.list_decisions()) == 1


class TestSetPreference:
    def test_sets_a_new_preference(self) -> None:
        wm.set_preference("тон", "директен")
        assert wm.list_preferences() == {"тон": "директен"}

    def test_same_topic_overwrites(self) -> None:
        wm.set_preference("тон", "директен")
        wm.set_preference("тон", "неформален")
        assert wm.list_preferences() == {"тон": "неформален"}

    def test_missing_topic_or_value_is_rejected(self) -> None:
        assert "Нужни са" in wm.set_preference("", "x")
        assert "Нужни са" in wm.set_preference("topic", "")

    def test_same_value_under_a_new_topic_merges_into_the_old_one(self) -> None:
        wm.set_preference("communication style", "be direct")
        msg = wm.set_preference("tone", "be direct")
        assert "Обединено" in msg
        assert wm.list_preferences() == {"communication style": "be direct"}


class TestBriefing:
    def test_empty_state_is_an_empty_string(self) -> None:
        assert wm.briefing() == ""

    def test_open_thread_with_next_step_is_shown(self) -> None:
        wm.add_thread("build a thing", next_step="write tests")
        out = wm.briefing()
        assert "build a thing" in out
        assert "СЛЕДВА: write tests" in out

    def test_blocked_thread_is_marked(self) -> None:
        tid = wm.add_thread("blocked task").split("#")[1].split(":")[0]
        wm.update_thread(tid, status="blocked", next_step="waiting on X")
        out = wm.briefing()
        assert "⛔" in out
        assert "блокирано: waiting on X" in out

    def test_decisions_and_preferences_included(self) -> None:
        wm.add_decision("use SQLite", why="simple")
        wm.set_preference("тон", "директен")
        out = wm.briefing()
        assert "use SQLite" in out and "защото: simple" in out
        assert "тон: директен" in out

    def test_stale_threads_are_shown_separately(self) -> None:
        # A scoped MonkeyPatch context, not the test's own `monkeypatch`
        # fixture: undo()/leaving the `with` block must only revert `_now`,
        # never the autouse fixture's DB_PATH isolation (the two share the
        # same underlying fixture instance within one test otherwise).
        old_ts = (datetime.now(timezone.utc) - timedelta(days=wm.STALE_DAYS + 1)).isoformat(timespec="seconds")
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(wm, "_now", lambda: old_ts)
            wm.add_thread("ancient task")
        out = wm.briefing()
        assert "Заспали нишки" in out
        assert "ancient task" in out
        assert "Отворена работа" not in out  # nothing fresh, only stale


class TestAutoCapture:
    def test_fewer_than_two_messages_is_a_no_op(self) -> None:
        result = wm.auto_capture([{"role": "user", "content": "hi"}])
        assert result == {"decisions": 0, "preferences": 0, "threads": 0}

    def test_extracts_and_writes_decisions_preferences_threads(self, monkeypatch) -> None:
        class _Reply:
            raw_text = (
                '{"decisions": [{"what": "use pytest", "why": "standard"}], '
                '"preferences": [{"topic": "tone", "value": "direct"}], '
                '"threads": [{"title": "write docs", "next_step": "draft outline"}]}'
            )

        monkeypatch.setattr("genesis_agent.brain.Brain.complete", lambda self, messages: _Reply())
        messages = [
            {"role": "user", "content": "let's use pytest, be direct with me, and I still need to write docs"},
            {"role": "assistant", "content": "got it"},
        ]
        result = wm.auto_capture(messages)
        assert result == {"decisions": 1, "preferences": 1, "threads": 1}
        assert wm.list_decisions()[0]["what"] == "use pytest"
        assert wm.list_preferences() == {"tone": "direct"}
        assert wm.list_threads("open")[0]["title"] == "write docs"

    def test_strips_markdown_code_fence_around_json(self, monkeypatch) -> None:
        class _Reply:
            raw_text = '```json\n{"decisions": [{"what": "fenced"}], "preferences": [], "threads": []}\n```'

        monkeypatch.setattr("genesis_agent.brain.Brain.complete", lambda self, messages: _Reply())
        messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        result = wm.auto_capture(messages)
        assert result["decisions"] == 1

    def test_non_json_reply_returns_zero_counts_without_raising(self, monkeypatch) -> None:
        class _Reply:
            raw_text = "I couldn't extract anything meaningful."

        monkeypatch.setattr("genesis_agent.brain.Brain.complete", lambda self, messages: _Reply())
        messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        assert wm.auto_capture(messages) == {"decisions": 0, "preferences": 0, "threads": 0}

    def test_brain_exception_returns_zero_counts_without_raising(self, monkeypatch) -> None:
        def _boom(self, messages):
            raise RuntimeError("provider down")

        monkeypatch.setattr("genesis_agent.brain.Brain.complete", _boom)
        messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        assert wm.auto_capture(messages) == {"decisions": 0, "preferences": 0, "threads": 0}

    def test_extraction_failure_is_logged_not_silent(self, monkeypatch, caplog) -> None:
        """A real failure (model down, rate-limited, bad JSON) must be
        distinguishable from "genuinely nothing new to remember" — both
        return zero counts, but only one of them should leave a trace."""
        def _boom(self, messages):
            raise RuntimeError("provider down")

        monkeypatch.setattr("genesis_agent.brain.Brain.complete", _boom)
        messages = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        with caplog.at_level("WARNING", logger="genesis.workspace_memory"):
            wm.auto_capture(messages)
        assert "auto_capture" in caplog.text
        assert "provider down" in caplog.text


class TestStaleThreadsAndCloseThread:
    def test_stale_threads_lists_only_old_ones(self) -> None:
        old_ts = (datetime.now(timezone.utc) - timedelta(days=wm.STALE_DAYS + 5)).isoformat(timespec="seconds")
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(wm, "_now", lambda: old_ts)
            wm.add_thread("old task")
        wm.add_thread("fresh task")
        stale = wm.stale_threads()
        assert [t["title"] for t in stale] == ["old task"]

    def test_close_thread_marks_done_by_default(self) -> None:
        wm.add_thread("finish me")
        tid = wm.list_threads("open")[0]["id"]
        wm.close_thread(tid)
        assert wm.list_threads("done")[0]["title"] == "finish me"

    def test_close_thread_with_drop_deletes_it(self) -> None:
        wm.add_thread("bogus thread")
        tid = wm.list_threads("open")[0]["id"]
        wm.close_thread(tid, drop=True)
        assert wm.list_threads("all") == []

    def test_close_thread_invalid_id(self) -> None:
        assert "Невалиден id" in wm.close_thread("not-a-number")


class TestStats:
    def test_counts_across_categories(self) -> None:
        wm.add_thread("open one")
        tid = wm.add_thread("blocked one").split("#")[1].split(":")[0]
        wm.update_thread(tid, status="blocked")
        wm.add_decision("a decision")
        wm.set_preference("topic", "value")
        stats = wm.stats()
        assert stats == {"open": 1, "blocked": 1, "done": 0, "decisions": 1, "preferences": 1}


class TestPerWorkspace:
    """Нишките и решенията са на папката; предпочитанията са общи
    (bench_projects 2026-09-26: решение „създаден money.py“ от друга папка
    накара модела да търси несъществуващия файл вместо да го напише)."""

    def test_another_folder_does_not_see_this_folders_work(self, tmp_path) -> None:
        wm.add_thread("build money.py", next_step="write tests")
        wm.add_decision("created money.py with to_eur()")
        wm.set_preference("език", "български")
        wm.set_workspace(tmp_path / "project-b")
        out = wm.briefing()
        assert "money.py" not in out
        assert "език: български" in out
        assert wm.list_threads() == [] and wm.list_decisions() == []
        assert wm.stats()["open"] == 0 and wm.stats()["decisions"] == 0

    def test_the_same_folder_sees_it_again(self, tmp_path) -> None:
        wm.add_decision("use SQLite")
        wm.set_workspace(tmp_path / "project-b")
        wm.set_workspace(tmp_path / "project-a")
        assert "use SQLite" in wm.briefing()

    def test_same_title_in_two_folders_is_two_threads(self, tmp_path) -> None:
        wm.add_thread("write the README")
        wm.set_workspace(tmp_path / "project-b")
        assert "✓" in wm.add_thread("write the README")

    def test_the_folder_is_normalised(self, tmp_path) -> None:
        wm.add_decision("keep it")
        wm.set_workspace(str(tmp_path / "project-a") + "/")
        assert [d["what"] for d in wm.list_decisions()] == ["keep it"]

    def test_an_old_database_gains_the_column_and_hides_its_rows(self) -> None:
        import sqlite3
        with sqlite3.connect(wm.DB_PATH) as c:
            c.executescript(
                "CREATE TABLE threads (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,"
                " status TEXT NOT NULL DEFAULT 'open', next_step TEXT NOT NULL DEFAULT '',"
                " notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
                "CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, what TEXT NOT NULL,"
                " why TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);"
                "INSERT INTO threads (title, created_at, updated_at) VALUES ('old', 'x', 'x');"
                "INSERT INTO decisions (what, created_at) VALUES ('old decision', 'x');")
        assert wm.briefing() == ""  # не се знае на кой проект са
        wm.add_decision("new one")
        assert [d["what"] for d in wm.list_decisions()] == ["new one"]

    def test_genesis_skills_moves_the_memory_with_the_tools(self, monkeypatch, tmp_path) -> None:
        import genesis_skills
        monkeypatch.setattr(genesis_skills, "_WORKSPACE", genesis_skills._WORKSPACE)
        wm.add_decision("only in a")
        genesis_skills.set_workspace(tmp_path / "project-b")
        assert wm.current_workspace() == wm._norm(tmp_path / "project-b")
        assert wm.list_decisions() == []
