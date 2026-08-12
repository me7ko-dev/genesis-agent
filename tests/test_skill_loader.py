"""genesis_agent.skill_loader.skill_view — signature verification (2026-08-12).

skills_manager.save_skill() now signs NEW skills; skill_view() is the single
choke point everything that executes a skill's code goes through (USE_SKILL,
run_skill, load_skill_code) — so this is where a tampered-but-signed skill
must be refused, while every skill saved before signing existed (no
signature at all) keeps loading exactly as it always has.
"""
from __future__ import annotations

import json

import pytest

from genesis_agent import skill_loader as sl
from genesis_agent import skills_manager as sm


@pytest.fixture
def _isolated_keys(tmp_path_factory, monkeypatch):
    pytest.importorskip("cryptography")
    from genesis_agent import cryptography_utils as cu
    key_dir = tmp_path_factory.mktemp("keys")
    monkeypatch.setattr(cu, "KEY_DIR", key_dir)
    monkeypatch.setattr(cu, "PRIVATE_KEY_PATH", key_dir / "private_key.pem")
    monkeypatch.setattr(cu, "PUBLIC_KEY_PATH", key_dir / "public_key.pem")
    return cu


@pytest.fixture
def _isolated_skills(tmp_path, monkeypatch):
    """Redirects both skills_manager (writer) and skill_loader (reader) at
    the same throwaway directory, and resets skill_loader's module-level
    index cache — real fixtures write skills.json into it as part of the
    test, so a stale cache from an earlier test would see a different file."""
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(sm, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(sm, "SKILLS_ROOT", tmp_path)
    monkeypatch.setattr(sl, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(sl, "SKILLS_ROOT", tmp_path)
    monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", None)
    return skills_dir


def _tamper_code_block(md_path, new_code: str) -> None:
    """Rewrites just the fenced python block, leaving frontmatter untouched —
    simulates someone hand-editing the .md file after it was signed."""
    import re
    text = md_path.read_text(encoding="utf-8")
    text = re.sub(r"```python\n.*?\n```", f"```python\n{new_code}\n```", text, flags=re.DOTALL)
    md_path.write_text(text, encoding="utf-8")


class TestBackwardCompatNoSignature:
    def test_unsigned_skill_loads_normally(self, _isolated_skills) -> None:
        """Signing failed / was never attempted (e.g. crypto unavailable at
        save time) — signature is "", skill_view must not care at all."""
        sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        # Force it back to "never signed" regardless of whether crypto ran.
        idx_path = sm.SKILLS_DIR / sm.SKILLS_INDEX_NAME
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        idx["skills"][0]["signature"] = ""
        idx_path.write_text(json.dumps(idx), encoding="utf-8")
        sl.reload_skills_index()

        data = sl.skill_view("fibonacci")
        assert data["code"] == "print(1)"

    def test_unsigned_skill_loads_even_after_the_code_is_edited(self, _isolated_skills) -> None:
        """The whole point of 'unsigned = old behavior': no integrity promise
        was ever made for it, so an edit is not an error here."""
        path = sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        idx_path = sm.SKILLS_DIR / sm.SKILLS_INDEX_NAME
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        idx["skills"][0]["signature"] = ""
        idx_path.write_text(json.dumps(idx), encoding="utf-8")
        sl.reload_skills_index()

        _tamper_code_block(path, "print('tampered')")
        data = sl.skill_view("fibonacci")
        assert data["code"] == "print('tampered')"


class TestSignedSkillVerification:
    def test_signed_skill_with_untouched_code_loads_fine(self, _isolated_skills, _isolated_keys) -> None:
        sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        data = sl.skill_view("fibonacci")
        assert data["code"] == "print(1)"

    def test_signed_skill_with_tampered_code_is_refused(self, _isolated_skills, _isolated_keys) -> None:
        path = sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        _tamper_code_block(path, "import os\nos.system('rm -rf /')")
        with pytest.raises(ValueError, match="подпис"):
            sl.skill_view("fibonacci")

    def test_signed_skill_with_edited_index_signature_is_refused(
        self, _isolated_skills, _isolated_keys
    ) -> None:
        """The reverse tamper: code on disk is untouched, but someone hand-
        edited (or corrupted) the stored signature in skills.json."""
        sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        idx_path = sm.SKILLS_DIR / sm.SKILLS_INDEX_NAME
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        idx["skills"][0]["signature"] = "aa" * 256  # well-formed hex, wrong signature
        idx_path.write_text(json.dumps(idx), encoding="utf-8")
        sl.reload_skills_index()

        with pytest.raises(ValueError, match="подпис"):
            sl.skill_view("fibonacci")

    def test_verification_fails_open_when_crypto_tooling_errors(
        self, _isolated_skills, _isolated_keys, monkeypatch
    ) -> None:
        """A signature IS present, but verify_signature() itself blows up
        (e.g. cryptography package went missing between save and load) — must
        not turn into every existing signed skill suddenly refusing to run."""
        sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")

        def _boom(_code, _sig):
            raise RuntimeError("cryptography backend unavailable")
        monkeypatch.setattr("genesis_agent.cryptography_utils.verify_signature", _boom)

        data = sl.skill_view("fibonacci")
        assert data["code"] == "print(1)"

    def test_use_skill_refuses_a_tampered_signed_skill(self, _isolated_skills, _isolated_keys) -> None:
        """End-to-end through the actual USE_SKILL entry point, not just the
        skill_view() unit — this is the path a real tool call goes through."""
        path = sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        _tamper_code_block(path, "print('tampered')")
        out = sl.use_skill("fibonacci")
        assert "Грешка при зареждане" in out
        assert "подпис" in out


class TestMissingKeyIsNotTampering:
    """Found by running a real mission end-to-end (2026-08-12).

    verify_signature() returns False both when a signature genuinely does not
    match AND when there is no public key to check it against. Treating those
    the same meant any clone without the keys — or merely a mistyped
    GENESIS_HOME — made every signed skill unloadable, and said the code had
    been tampered with while doing it. Absence of evidence is not evidence of
    tampering: with no key we know nothing, so a signed skill is treated
    exactly like an unsigned one. A mismatch against a key that IS present
    stays a hard refusal.
    """

    def test_signed_skill_loads_when_no_key_is_available(
        self, _isolated_skills, _isolated_keys, monkeypatch
    ) -> None:
        sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        # Key vanishes (fresh clone / wrong GENESIS_HOME / not yet generated).
        monkeypatch.setattr(_isolated_keys, "PUBLIC_KEY_PATH",
                            _isolated_keys.KEY_DIR / "does-not-exist.pem")
        assert sl.skill_view("fibonacci")["code"] == "print(1)"

    def test_tampering_is_still_refused_when_the_key_is_present(
        self, _isolated_skills, _isolated_keys
    ) -> None:
        path = sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        _tamper_code_block(path, "import os\nos.system('rm -rf /')")
        with pytest.raises(ValueError, match="подпис"):
            sl.skill_view("fibonacci")

    def test_the_refusal_names_both_possible_causes(
        self, _isolated_skills, _isolated_keys
    ) -> None:
        """A key mismatch and an edited file produce the identical symptom, so
        the message must not assert the scarier one as fact."""
        path = sm.save_skill(slug="fibonacci", code="print(1)", goal="fibonacci helper")
        _tamper_code_block(path, "print('tampered')")
        with pytest.raises(ValueError) as exc:
            sl.skill_view("fibonacci")
        msg = str(exc.value)
        assert "променян" in msg
        assert "ДРУГ ключ" in msg
