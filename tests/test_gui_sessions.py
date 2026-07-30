"""genesis_agent.gui.gui_sessions — SQLite-backed chat session persistence
for the GTK app's Recents sidebar, previously untested. DB_PATH is redirected
to tmp_path for every test (see conftest.py's convention: patch the module's
own path constant, not the env var, since it's already bound at import time)."""
from __future__ import annotations

import pytest

from genesis_agent.gui import gui_sessions as gs


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    monkeypatch.setattr(gs, "DB_PATH", tmp_path / "gui_sessions.db")
    yield


class TestSaveAndLoad:
    def test_round_trip(self) -> None:
        sid = gs.new_id()
        messages = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        gs.save(sid, messages)
        assert gs.load(sid) == messages

    def test_loading_an_unknown_id_returns_none(self) -> None:
        assert gs.load("does-not-exist") is None

    def test_system_messages_are_stripped_before_persisting(self) -> None:
        sid = gs.new_id()
        gs.save(sid, [
            {"role": "system", "content": "you are Genesis"},
            {"role": "user", "content": "hi"},
        ])
        assert gs.load(sid) == [{"role": "user", "content": "hi"}]

    def test_saving_only_a_system_message_is_a_no_op(self) -> None:
        sid = gs.new_id()
        gs.save(sid, [{"role": "system", "content": "you are Genesis"}])
        assert gs.load(sid) is None

    def test_resaving_the_same_id_updates_in_place(self) -> None:
        sid = gs.new_id()
        gs.save(sid, [{"role": "user", "content": "first"}])
        gs.save(sid, [{"role": "user", "content": "first"}, {"role": "assistant", "content": "second"}])
        assert gs.load(sid) == [{"role": "user", "content": "first"}, {"role": "assistant", "content": "second"}]
        assert len(gs.list_recent()) == 1


class TestTitleDerivation:
    def test_title_comes_from_the_first_user_message(self) -> None:
        sid = gs.new_id()
        gs.save(sid, [
            {"role": "assistant", "content": "ignored, not user"},
            {"role": "user", "content": "build me a fizzbuzz script"},
            {"role": "user", "content": "second user message, ignored"},
        ])
        [row] = gs.list_recent()
        assert row["title"] == "build me a fizzbuzz script"

    def test_title_collapses_internal_whitespace(self) -> None:
        sid = gs.new_id()
        gs.save(sid, [{"role": "user", "content": "hello\n\n   world  \t again"}])
        [row] = gs.list_recent()
        assert row["title"] == "hello world again"

    def test_title_is_truncated_with_ellipsis(self) -> None:
        sid = gs.new_id()
        long_text = "x" * 100
        gs.save(sid, [{"role": "user", "content": long_text}])
        [row] = gs.list_recent()
        assert row["title"] == "x" * 48 + "…"

    def test_falls_back_when_no_user_message_has_content(self) -> None:
        sid = gs.new_id()
        gs.save(sid, [{"role": "user", "content": ""}, {"role": "assistant", "content": "hi"}])
        [row] = gs.list_recent()
        assert row["title"] == "Нов разговор"


class TestListRecent:
    def test_orders_most_recently_updated_first(self, monkeypatch) -> None:
        # save()'s timestamp has 1-second resolution — two saves in the same
        # test tick would otherwise tie and fall back to an unspecified order.
        older, newer = gs.new_id(), gs.new_id()
        monkeypatch.setattr(gs, "_now", lambda: "2026-01-01T00:00:00+00:00")
        gs.save(older, [{"role": "user", "content": "first session"}])
        monkeypatch.setattr(gs, "_now", lambda: "2026-01-01T00:00:05+00:00")
        gs.save(newer, [{"role": "user", "content": "second session"}])
        rows = gs.list_recent()
        assert [r["id"] for r in rows] == [newer, older]

    def test_respects_the_limit(self) -> None:
        for i in range(5):
            gs.save(gs.new_id(), [{"role": "user", "content": f"session {i}"}])
        assert len(gs.list_recent(limit=2)) == 2


class TestDelete:
    def test_removes_the_session(self) -> None:
        sid = gs.new_id()
        gs.save(sid, [{"role": "user", "content": "hi"}])
        gs.delete(sid)
        assert gs.load(sid) is None

    def test_deleting_an_unknown_id_does_not_raise(self) -> None:
        gs.delete("does-not-exist")
