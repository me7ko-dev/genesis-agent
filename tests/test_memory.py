"""genesis_agent.memory — the persistent key/value store, previously untested.

The prefix filter fed its argument straight into SQL LIKE, where `_` matches
any single character and `%` matches anything (fixed 2026-08-12). Since this
store's own conventions are snake_case (`user_name`, `preferred_language`,
`coding_style`, `last_project`), nearly every realistic prefix contained a `_`
and silently matched more keys than asked for.
"""
from __future__ import annotations

import pytest

from genesis_agent import memory as mem


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """memory.py binds DB_PATH at import; point it at a throwaway file so the
    real persistent_memory.db is never touched."""
    monkeypatch.setattr(mem, "DB_PATH", tmp_path / "kv.db")
    yield


class TestStoreAndRecall:
    def test_round_trips_a_string(self) -> None:
        mem.memory_store("name", "Genesis")
        assert mem.memory_recall("name") == "Genesis"

    def test_round_trips_structured_values(self) -> None:
        mem.memory_store("style", {"indent": 4, "quotes": "double"})
        assert mem.memory_recall("style") == {"indent": 4, "quotes": "double"}

    def test_round_trips_a_list(self) -> None:
        mem.memory_store("tags", ["a", "b"])
        assert mem.memory_recall("tags") == ["a", "b"]

    def test_missing_key_returns_the_default(self) -> None:
        assert mem.memory_recall("nope", default="fallback") == "fallback"

    def test_storing_the_same_key_twice_updates_in_place(self) -> None:
        mem.memory_store("k", "first")
        mem.memory_store("k", "second")
        assert mem.memory_recall("k") == "second"
        assert mem.memory_list_keys() == ["k"]

    def test_delete_reports_whether_it_found_anything(self) -> None:
        mem.memory_store("k", "v")
        assert mem.memory_delete("k") is True
        assert mem.memory_delete("k") is False
        assert mem.memory_recall("k") is None


class TestListKeysPrefixIsLiteral:
    """The bug: a prefix is a literal string, not a LIKE pattern."""

    def _seed(self) -> None:
        for k in ("last_project", "lastZproject", "user_name", "userXname",
                  "users_list", "unrelated"):
            mem.memory_store(k, "v")

    def test_underscore_is_not_a_single_character_wildcard(self) -> None:
        self._seed()
        assert mem.memory_list_keys("last_") == ["last_project"]

    def test_underscore_wildcard_does_not_leak_across_similar_names(self) -> None:
        self._seed()
        assert mem.memory_list_keys("user_") == ["user_name"]

    def test_percent_in_a_prefix_is_literal_too(self) -> None:
        mem.memory_store("100%_done", "v")
        mem.memory_store("100Xdone", "v")
        assert mem.memory_list_keys("100%") == ["100%_done"]

    def test_a_percent_prefix_does_not_match_everything(self) -> None:
        self._seed()
        assert mem.memory_list_keys("%") == []

    def test_backslash_in_a_prefix_is_literal(self) -> None:
        mem.memory_store(r"path\to", "v")
        mem.memory_store("pathXto", "v")
        assert mem.memory_list_keys("path\\") == [r"path\to"]

    def test_plain_prefix_still_works(self) -> None:
        self._seed()
        assert mem.memory_list_keys("last") == ["lastZproject", "last_project"]

    def test_no_prefix_returns_everything_sorted(self) -> None:
        self._seed()
        keys = mem.memory_list_keys()
        assert keys == sorted(keys)
        assert len(keys) == 6

    def test_prefix_matching_nothing_is_empty(self) -> None:
        self._seed()
        assert mem.memory_list_keys("zzz") == []


class TestDump:
    def test_dump_decodes_values(self) -> None:
        mem.memory_store("a", 1)
        mem.memory_store("b", {"x": 2})
        assert mem.memory_dump() == {"a": 1, "b": {"x": 2}}

    def test_empty_store_dumps_empty(self) -> None:
        assert mem.memory_dump() == {}
