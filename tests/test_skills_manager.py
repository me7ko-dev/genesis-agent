"""genesis_agent.skills_manager — slug collision handling. slugify() truncates
to 48 chars, so two different goals sharing a long common prefix (e.g. the
templated goal_engine topics) used to silently collide and the second save
overwrote the first's .md file and index entry (real bug, caught manually
2026-07-26, commit 2a87e9b). SKILLS_DIR/SKILLS_ROOT are monkeypatched to a
tmp directory so tests never touch the real skills library."""
from __future__ import annotations

import json

import pytest

from genesis_agent import skills_manager as sm


def _isolate(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(sm, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(sm, "SKILLS_ROOT", tmp_path)
    return skills_dir


@pytest.fixture
def _isolated_keys(tmp_path_factory, monkeypatch):
    """save_skill() now signs with cryptography_utils — redirect its key
    storage to a throwaway directory so tests never touch the real
    ~/.genesis/private_key.pem, same isolation as test_cryptography_utils.py."""
    cryptography = pytest.importorskip("cryptography")
    from genesis_agent import cryptography_utils as cu
    key_dir = tmp_path_factory.mktemp("keys")
    monkeypatch.setattr(cu, "KEY_DIR", key_dir)
    monkeypatch.setattr(cu, "PRIVATE_KEY_PATH", key_dir / "private_key.pem")
    monkeypatch.setattr(cu, "PUBLIC_KEY_PATH", key_dir / "public_key.pem")
    return cu


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


# ── Signing new skills (2026-08-12) ─────────────────────────────────────────
# save_skill() now signs the code with cryptography_utils.sign_code and
# stores the signature in the index entry — skill_loader.skill_view() is
# where it actually gets checked before anything executes (test_skill_loader.py).

class TestSigning:
    def test_saved_skill_has_a_non_empty_signature(self, tmp_path, monkeypatch, _isolated_keys) -> None:
        skills_dir = _isolate(tmp_path, monkeypatch)
        sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        idx = _index(skills_dir)
        assert idx["skills"][0]["signature"]

    def test_signature_actually_verifies_against_the_saved_code(
        self, tmp_path, monkeypatch, _isolated_keys
    ) -> None:
        skills_dir = _isolate(tmp_path, monkeypatch)
        sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        entry = _index(skills_dir)["skills"][0]
        assert _isolated_keys.verify_signature("print(1)", entry["signature"]) is True

    def test_two_different_skills_get_different_signatures(
        self, tmp_path, monkeypatch, _isolated_keys
    ) -> None:
        skills_dir = _isolate(tmp_path, monkeypatch)
        sm.save_skill(slug="a", code="print('a')", goal="goal A")
        sm.save_skill(slug="b", code="print('b')", goal="goal B")
        sigs = [s["signature"] for s in _index(skills_dir)["skills"]]
        assert sigs[0] != sigs[1]

    def test_atomic_index_write_leaves_no_temp_files_behind(self, tmp_path, monkeypatch, _isolated_keys) -> None:
        skills_dir = _isolate(tmp_path, monkeypatch)
        sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        leftovers = [p.name for p in skills_dir.iterdir() if ".tmp" in p.name]
        assert leftovers == []

    def test_a_crash_mid_write_cannot_truncate_the_index(self, tmp_path, monkeypatch) -> None:
        """The durability point of writing through a temp file + os.replace:
        the index either stays at its previous complete state or becomes the
        new complete state, never a half-written one. A truncated skills.json
        is unusually costly here — _load_index does not catch JSONDecodeError,
        so every later save_skill raises, and skill_loader catches it and
        returns an EMPTY index, making the whole library invisible at once
        even though every .md file is still on disk."""
        skills_dir = _isolate(tmp_path, monkeypatch)
        sm.save_skill(slug="first", code="print(1)", goal="the first skill")
        good = (skills_dir / sm.SKILLS_INDEX_NAME).read_text(encoding="utf-8")

        # Simulate the process dying after the temp file is written but before
        # the rename makes it visible.
        def _die(*_a, **_kw):
            raise KeyboardInterrupt("killed mid-write")
        monkeypatch.setattr(sm.os, "replace", _die)

        with pytest.raises(KeyboardInterrupt):
            sm.save_skill(slug="second", code="print(2)", goal="the second skill")

        # The index on disk is still the previous, entirely valid one.
        assert (skills_dir / sm.SKILLS_INDEX_NAME).read_text(encoding="utf-8") == good
        assert json.loads(good)["skills"][0]["name"] == "first"

    def test_signing_failure_does_not_block_saving_the_skill(self, tmp_path, monkeypatch) -> None:
        """Fail-open: an optional integrity upgrade must not turn into 'my
        skill library stopped saving' the moment the crypto package or key
        generation hiccups — same convention as every other optional feature
        in this codebase (embeddings, ruff, web_search caching, ...)."""
        skills_dir = _isolate(tmp_path, monkeypatch)

        def _boom(_code):
            raise RuntimeError("no cryptography backend available")
        monkeypatch.setattr("genesis_agent.cryptography_utils.sign_code", _boom)

        path = sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        assert path.exists()
        entry = _index(skills_dir)["skills"][0]
        assert entry["signature"] == ""
