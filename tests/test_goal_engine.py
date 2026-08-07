"""genesis_agent.goal_engine — feeds the Discord bot's `!start24_7` loop and
`parallel_forge.py` (both real, documented entrypoints) with gap-driven goals
instead of a static list. Zero coverage before this file.

What matters most: next_goals() must never hand back a goal that duplicates
an existing skill or a goal already tried before (that's the whole point of
gap-driven generation over a static list), must respect `n`, and must never
raise on a missing/corrupt skills.json or next_goals.json — those are normal
first-run states, not error states.
"""
from __future__ import annotations

import json

import pytest

import genesis_agent.goal_engine as ge


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    monkeypatch.setattr(ge, "SKILLS_JSON", tmp_path / "skills.json")
    monkeypatch.setattr(ge, "DATA_DIR", tmp_path)


def _write_skills(path, names: list[str]) -> None:
    path.write_text(json.dumps({"skills": [{"name": n} for n in names]}), encoding="utf-8")


class TestLoadSkillNames:
    def test_missing_file_returns_empty_list(self) -> None:
        assert ge._load_skill_names() == []

    def test_corrupt_json_returns_empty_list_not_an_exception(self) -> None:
        ge.SKILLS_JSON.write_text("{not valid json", encoding="utf-8")
        assert ge._load_skill_names() == []

    def test_reads_names_from_skills_list(self) -> None:
        _write_skills(ge.SKILLS_JSON, ["retry_backoff", "csv_parser"])
        assert ge._load_skill_names() == ["retry_backoff", "csv_parser"]


class TestLoadPastGoals:
    def test_missing_file_returns_empty_set(self) -> None:
        assert ge._load_past_goals() == set()

    def test_corrupt_json_returns_empty_set(self) -> None:
        (ge.DATA_DIR / "next_goals.json").write_text("not json", encoding="utf-8")
        assert ge._load_past_goals() == set()

    def test_returns_slugified_past_goals(self) -> None:
        (ge.DATA_DIR / "next_goals.json").write_text(
            json.dumps(["Implement an LRU cache"]), encoding="utf-8")
        assert ge._load_past_goals() == {ge.slugify("Implement an LRU cache")}


class TestCoverageReport:
    def test_counts_skills_matching_domain_keywords(self) -> None:
        _write_skills(ge.SKILLS_JSON, [
            "exponential_backoff_retry",   # matches reliability: "retry"
            "csv_schema_validator",        # matches data: "csv"
            "totally_unrelated_thing",     # matches nothing
        ])
        report = ge.coverage_report()
        assert report["reliability"] == 1
        assert report["data"] == 1
        assert report["text"] == 0

    def test_empty_skills_gives_zero_everywhere(self) -> None:
        report = ge.coverage_report()
        assert all(v == 0 for v in report.values())
        assert set(report) == set(ge._DOMAINS)


class TestNextGoals:
    def test_respects_n(self) -> None:
        goals = ge.next_goals(3, use_semantic=False)
        assert len(goals) == 3

    def test_never_duplicates_within_one_call(self) -> None:
        goals = ge.next_goals(15, use_semantic=False)
        assert len(goals) == len(set(goals))

    def test_skips_goals_matching_an_existing_skill_slug(self) -> None:
        # The highest-priority domain (reliability, weight=5) generates
        # "Implement a production-grade exponential backoff retry utility...".
        # Pre-register that exact skill and confirm next_goals() steps past it.
        first_batch = ge.next_goals(1, use_semantic=False)
        assert len(first_batch) == 1
        slug = ge.slugify(first_batch[0])
        _write_skills(ge.SKILLS_JSON, [slug])

        second_batch = ge.next_goals(1, use_semantic=False)
        assert second_batch != first_batch

    def test_skips_goals_matching_a_past_goal(self) -> None:
        first_batch = ge.next_goals(1, use_semantic=False)
        (ge.DATA_DIR / "next_goals.json").write_text(
            json.dumps(first_batch), encoding="utf-8")

        second_batch = ge.next_goals(1, use_semantic=False)
        assert second_batch != first_batch

    def test_use_semantic_false_never_touches_embeddings(self, monkeypatch) -> None:
        def _boom():
            raise AssertionError("embeddings.available() must not be called when use_semantic=False")
        monkeypatch.setattr("genesis_agent.embeddings.available", _boom)
        ge.next_goals(2, use_semantic=False)  # must not raise

    def test_low_coverage_high_weight_domain_is_prioritized(self) -> None:
        """reliability (weight=5) has no skills yet -> its topics should be
        the very first ones returned, ahead of lower-weight domains."""
        goals = ge.next_goals(1, use_semantic=False)
        assert goals[0].startswith("Implement a production-grade")  # reliability's template
