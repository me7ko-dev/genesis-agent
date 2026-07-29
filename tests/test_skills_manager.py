"""genesis_agent.skills_manager — slug collision handling. slugify() truncates
to 48 chars, so two different goals sharing a long common prefix (e.g. the
templated goal_engine topics) used to silently collide and the second save
overwrote the first's .md file and index entry (real bug, caught manually
2026-07-26, commit 2a87e9b). SKILLS_DIR/SKILLS_ROOT are monkeypatched to a
tmp directory so tests never touch the real skills library."""
from __future__ import annotations

import json

from genesis_agent import skills_manager as sm


def _isolate(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(sm, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(sm, "SKILLS_ROOT", tmp_path)
    return skills_dir


def _index(skills_dir) -> dict:
    return json.loads((skills_dir / sm.SKILLS_INDEX_NAME).read_text(encoding="utf-8"))


def test_slugify_basic() -> None:
    assert sm.slugify("Hello World!") == "hello_world"


def test_slugify_truncates_to_max_len() -> None:
    long_text = "a" * 100
    assert len(sm.slugify(long_text)) == 48


def test_slugify_empty_falls_back_to_skill() -> None:
    assert sm.slugify("!!!") == "skill"


def test_resaving_same_goal_updates_in_place(tmp_path, monkeypatch) -> None:
    skills_dir = _isolate(tmp_path, monkeypatch)
    long_prefix = "generate a function that does something with a very long shared prefix here"
    sm.save_skill(slug=long_prefix, code="print(1)", goal="build a fibonacci helper")
    sm.save_skill(slug=long_prefix, code="print(2)", goal="build a fibonacci helper")
    idx = _index(skills_dir)
    matching = [s for s in idx["skills"] if s["description"] == "build a fibonacci helper"]
    assert len(matching) == 1  # same goal → updated in place, not duplicated


def test_different_goal_with_colliding_slug_gets_disambiguated(tmp_path, monkeypatch) -> None:
    """The exact bug: same 48-char-truncated slug, DIFFERENT goal → must not
    silently overwrite the first skill."""
    skills_dir = _isolate(tmp_path, monkeypatch)
    shared_prefix = "generate a function that does something with a very long shared prefix here"
    sm.save_skill(slug=shared_prefix, code="print('fib')", goal="build a fibonacci helper")
    sm.save_skill(slug=shared_prefix, code="print('flat')", goal="build a flatten-list helper")

    idx = _index(skills_dir)
    assert len(idx["skills"]) == 2
    goals = {s["description"] for s in idx["skills"]}
    assert goals == {"build a fibonacci helper", "build a flatten-list helper"}
    names = {s["name"] for s in idx["skills"]}
    assert len(names) == 2  # disambiguated, not collided

    base_slug = sm.slugify(shared_prefix)
    assert base_slug in names  # first save keeps the plain base slug
    for name in names - {base_slug}:
        assert name.startswith(base_slug[:40] + "_")  # second save is hash-suffixed


def test_disambiguated_skill_md_file_actually_written(tmp_path, monkeypatch) -> None:
    skills_dir = _isolate(tmp_path, monkeypatch)
    shared_prefix = "generate a function that does something with a very long shared prefix here"
    sm.save_skill(slug=shared_prefix, code="print('a')", goal="goal A")
    sm.save_skill(slug=shared_prefix, code="print('b')", goal="goal B")
    idx = _index(skills_dir)
    for entry in idx["skills"]:
        md_path = skills_dir.parent / entry["file_path"]
        assert md_path.exists()


def test_unrelated_slugs_never_collide(tmp_path, monkeypatch) -> None:
    skills_dir = _isolate(tmp_path, monkeypatch)
    sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
    sm.save_skill(slug="flatten list", code="print(2)", goal="flatten list helper")
    idx = _index(skills_dir)
    assert len(idx["skills"]) == 2
