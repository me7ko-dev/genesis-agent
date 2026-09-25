"""genesis_agent.skill_loader.skill_view — signature verification (2026-08-12).

skills_manager.save_skill() now signs NEW skills; skill_view() is the single
choke point everything that executes a skill's code goes through (USE_SKILL,
run_skill) — so this is where a tampered-but-signed skill
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

    def test_signature_survives_trailing_whitespace_in_the_saved_code(
        self, _isolated_skills, _isolated_keys
    ) -> None:
        """save_skill's markdown embedding does code.rstrip(), and
        skill_view()'s fence regex does a full .strip() reading it back —
        the signature must be computed on that same normalized string, or
        any code with trailing whitespace/newlines (routine for
        LLM-generated code) verifies against itself and always fails,
        wrongly refusing an untouched skill as "tampered" (bug found
        2026-09-18, fixed in save_skill's signing call)."""
        sm.save_skill(slug="fibonacci", code="print(1)\n\n   \n", goal="fibonacci helper")
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


class TestFuzzyResolveNeedsRealEvidence:
    """Found by tracing a real mission (2026-08-12).

    `search_skills` returns anything with a single overlapping word, and
    `resolve_skill` used to take candidates[0] no matter how weak. Live case:
    the query "in-process job queue retry exponential backoff" resolved to an
    EventBus skill on the strength of the word "process" alone, and USE_SKILL
    ran its self-test and answered "OK" — so the model believed a job-queue
    skill existed and spent 5 of its 8 rounds interrogating it.
    """

    def _bus(self):
        sm.save_skill(
            slug="build_a_stdlib_only_an_in_process_event_bus_pub",
            code="class EventBus:\n    def subscribe(self):\n        pass\n\nprint('OK')",
            goal="Build a stdlib-only, in-process event bus (pub/sub dispatcher)",
        )
        sl.reload_skills_index()

    def test_one_shared_word_does_not_resolve(self, _isolated_skills) -> None:
        self._bus()
        resolved, candidates = sl.resolve_skill(
            "in-process job queue retry exponential backoff")
        assert resolved is None
        # The near-miss is still reported, just not executed.
        assert [c["name"] for c in candidates] == [
            "build_a_stdlib_only_an_in_process_event_bus_pub"]

    def test_use_skill_says_no_match_instead_of_running_the_wrong_skill(
        self, _isolated_skills
    ) -> None:
        self._bus()
        out = sl.use_skill("in-process job queue retry exponential backoff")
        assert "Няма достатъчно близко умение" in out
        assert "напиши кода сам" in out.lower()
        # The wrong skill's own output must NOT appear as if it were an answer.
        assert "Достъпни:" not in out

    def test_two_shared_words_still_resolve(self, _isolated_skills) -> None:
        """The threshold must not break genuine fuzzy matching."""
        self._bus()
        resolved, _ = sl.resolve_skill("in-process event dispatcher")
        assert resolved == "build_a_stdlib_only_an_in_process_event_bus_pub"

    def test_exact_name_always_resolves_however_odd(self, _isolated_skills) -> None:
        """Escape hatch the refusal message points the model at: a skill named
        in full is never second-guessed by the relevance threshold."""
        self._bus()
        resolved, candidates = sl.resolve_skill(
            "build_a_stdlib_only_an_in_process_event_bus_pub")
        assert resolved == "build_a_stdlib_only_an_in_process_event_bus_pub"
        assert candidates == []


class TestSearchSeesTheOperatorsLanguage:
    """`_keywords` беше `[a-z0-9_]+` — само ASCII. Операторът пише на
    български, а `search_skills` е механизмът, по който изобщо се стига до
    преизползване на умение (когато embeddings липсват, а те са незадължителна
    зависимост, това е ЕДИНСТВЕНИЯТ механизъм).

    Измерено преди поправката: „искам умение за четене на конфигурационен
    файл“ → празно множество думи → score 0 за всяко умение → нито едно не
    може да се намери никога. Библиотеката съществува заради преизползването;
    на езика на оператора то беше изключено.
    """

    def test_a_cyrillic_query_produces_keywords_at_all(self) -> None:
        words = sl._keywords("обработка на CSV файлове")
        assert "обработка" in words
        assert "файлове" in words
        assert "csv" in words, "латиницата в смесен текст трябва да оцелее"

    def test_a_fully_cyrillic_query_is_no_longer_empty(self) -> None:
        assert sl._keywords("четене на конфигурационен файл")

    def test_template_verbs_are_filtered_in_bulgarian_too(self) -> None:
        """Английските шаблонни глаголи вече се махат („build“, „write“).
        Без същото за българските две напълно несвързани цели съвпадат само
        защото и двете започват с „Направи“."""
        assert sl._keywords("Направи нещо с това") == set()

    def test_a_cyrillic_trigger_can_be_found_by_a_cyrillic_query(
        self, _isolated_skills, monkeypatch
    ) -> None:
        """Целите от goals_from_real_work са на български, тоест уменията,
        които агентът сам създава, ще имат български тригери."""
        skills_dir = _isolated_skills
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "skills.json").write_text(json.dumps({"skills": [
            {"name": "obrabotka_na_otcheti", "file_path": "skills/x.md",
             "description": "Обработка на месечни отчети от CSV",
             "triggers": ["обработка отчети csv"]},
            {"name": "retry_backoff", "file_path": "skills/y.md",
             "description": "Exponential backoff retry helper",
             "triggers": ["retry backoff"]},
        ]}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", None)

        hits = sl.search_skills("обработка на отчети", top_n=3, use_semantic=False)
        assert hits, "нито едно умение не беше намерено по българска заявка"
        assert hits[0]["name"] == "obrabotka_na_otcheti"

    def test_english_search_is_unchanged(self, _isolated_skills, monkeypatch) -> None:
        skills_dir = _isolated_skills
        skills_dir.mkdir(parents=True, exist_ok=True)
        (skills_dir / "skills.json").write_text(json.dumps({"skills": [
            {"name": "retry_backoff", "file_path": "skills/y.md",
             "description": "Exponential backoff retry helper",
             "triggers": ["retry backoff"]},
        ]}, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", None)

        hits = sl.search_skills("exponential backoff retry", top_n=3, use_semantic=False)
        assert hits and hits[0]["name"] == "retry_backoff"
        assert hits[0]["_kw_score"] == 3


class TestUseSkillResolvesFromBulgarian:
    """Изискването на оператора, проверено там, където се решава: `USE_SKILL`
    минава през `resolve_skill`, а тя иска поне 2 съвпадащи думи, преди да
    изпълни умение. Това е прагът, който пази от увереното грешно умение
    (реален случай в коментара на функцията: „in-process job queue" резолвна
    до event bus само по думата „process" и изгори 5 от 8 рунда).

    Тоест българската заявка трябва да прескочи прага ЧЕСТНО — по истински
    съвпадащи думи, а не с понижен праг.
    """

    @pytest.fixture
    def _library(self, _isolated_skills, monkeypatch):
        from genesis_agent import skills_manager as sm
        code = ("import os\n\n"
                "def cleanup_temp_files(root='/tmp'):\n"
                "    return [p for p in os.listdir(root) if p.endswith('.tmp')]\n\n"
                "assert isinstance(cleanup_temp_files('/tmp'), list)\nprint('OK')\n")
        sm.save_skill(slug="Изчисти временните файлове по график", code=code,
                      goal="Изчисти временните файлове по график")
        monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", None)
        return _isolated_skills

    def test_a_bulgarian_request_resolves_to_the_english_named_skill(
        self, _library
    ) -> None:
        name, _ = sl.resolve_skill("изчисти временните файлове")
        assert name == "cleanup_temp_files"

    def test_a_longer_sentence_around_it_still_resolves(self, _library) -> None:
        name, _ = sl.resolve_skill("искам да изчистя временните файлове по график")
        assert name == "cleanup_temp_files"

    def test_the_exact_english_name_resolves_directly(self, _library) -> None:
        name, candidates = sl.resolve_skill("cleanup_temp_files")
        assert name == "cleanup_temp_files"
        assert candidates == [], "точното име не минава през търсене"

    def test_an_unrelated_bulgarian_request_resolves_to_nothing(self, _library) -> None:
        """Прагът важи еднакво за двата езика. Уверено грешно умение е
        по-скъпо от никакво: изходът му се представя като отговор на
        заявката."""
        name, _ = sl.resolve_skill("направи ми справка за продажбите")
        assert name is None

    def test_one_shared_word_is_not_enough_in_bulgarian_either(self, _library) -> None:
        name, _ = sl.resolve_skill("файлове")
        assert name is None


def test_format_skill_list_names_every_skill_and_marks_verified(monkeypatch) -> None:
    """`/skills` и `genesis skills` — без модел. На живо въпросът „какви
    умения имаш" изгори два рунда в USE_SKILL заявки, които нямаше как да
    сработят."""
    monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", {
        "zeta": {"verified": False, "description": "draft"},
        "alpha": {"verified": True, "description": "x" * 200},
    })
    out = sl.format_skill_list(width=20).splitlines()
    assert out[0] == "2 умения, 1 verified"
    assert out[1].startswith("  ✓ alpha — ") and len(out[1]) < 50
    assert out[2] == "  · zeta — draft"

# ── domain_context: провереното знание в чата ───────────────────────────────

@pytest.fixture
def _shipped_skills(monkeypatch):
    shipped = sl.Path(sl.__file__).resolve().parent / "skills"
    monkeypatch.setattr(sl, "SKILLS_DIR", shipped)
    monkeypatch.setattr(sl, "SKILLS_ROOT", shipped.parent)
    monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", None)
    yield
    sl._SKILLS_INDEX_CACHE = None


def test_an_egn_request_gets_the_verified_rules(_shipped_skills) -> None:
    """Истинската заявка от 2026-09-25, при която моделите обърнаха пола."""
    text = sl.domain_context(
        "Направи в текущата папка Python модул egn.py за българско ЕГН. Функция "
        "validate(egn) — 10 цифри, съществуваща дата, вярна контролна цифра.")
    assert "bg_egn_validate_and_decode" in text
    assert "ЧЕТНА → мъж" in text


def test_an_unrelated_request_gets_nothing(_shipped_skills) -> None:
    assert sl.domain_context("напиши rate limiter с asyncio и тестове") == ""


def test_a_general_skill_is_never_injected_in_chat(monkeypatch) -> None:
    hit = {"name": "event_bus", "category": "autonomous", "verified": True, "_kw_score": 9}
    monkeypatch.setattr(sl, "search_skills", lambda *a, **k: [hit])
    assert sl.domain_context("event bus pub sub") == ""


def test_a_weak_domain_match_is_not_injected(monkeypatch) -> None:
    hit = {"name": "bg_egn", "category": "domain", "verified": True, "_kw_score": 1}
    monkeypatch.setattr(sl, "search_skills", lambda *a, **k: [hit])
    assert sl.domain_context("номер") == ""


def test_the_shipped_egn_skill_passes_its_own_self_test(_shipped_skills, tmp_path) -> None:
    import subprocess
    import sys
    script = tmp_path / "bg_egn.py"
    script.write_text(sl.skill_view("bg_egn_validate_and_decode")["code"], encoding="utf-8")
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                       encoding="utf-8", timeout=60, check=False)
    assert r.returncode == 0 and r.stdout.strip() == "OK", r.stderr
