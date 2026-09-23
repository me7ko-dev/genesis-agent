"""Инварианти на genesis_agent.skills_manager — той пише библиотеката.

Провалите тук са тихи и трайни: презаписано умение не се забелязва, докато
не потрябва, а разминат индекс прави умение невидимо, без нищо да гръмне.
Историята на репото пази и двата случая — "Drop a skills.json entry whose
.md file was never committed" и колизионния бъг, отбелязан в самия
save_skill (2026-07-26).

Подредено по цена на провала:
  1. нищо записано не се губи — нито при колизия, нито при конкурентност;
  2. индексът и файловете на диска не се разминават;
  3. името, под което умението се записва, не може да излезе от директорията.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from genesis_agent import skills_manager as sm

_CODE = "def f():\n    return 1\n\n\nassert f() == 1\nprint('OK')\n"


@pytest.fixture(autouse=True)
def _isolated_library(tmp_path, monkeypatch):
    """Истинска, но собствена библиотека — тестовете никога не пипат тази на
    машината."""
    lib = tmp_path / "skills"
    lib.mkdir()
    monkeypatch.setattr(sm, "SKILLS_DIR", lib)
    # SKILLS_ROOT също: save_skill записва пътя в индекса като относителен
    # спрямо него (`md_path.relative_to(SKILLS_ROOT)`), така че подмяната само
    # на SKILLS_DIR оставя двете сочещи в различни дървета.
    monkeypatch.setattr(sm, "SKILLS_ROOT", tmp_path)
    monkeypatch.setattr(sm, "_index_path", lambda: lib / "skills.json")
    # Подписването иска ключове; тук проверяваме индекса, не криптографията.
    monkeypatch.setattr(sm, "_sign", lambda *a, **kw: "", raising=False)
    return lib


def _index(lib) -> list[dict]:
    p = lib / "skills.json"
    if not p.is_file():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("skills", [])


class TestNothingWrittenIsEverLost:
    def test_two_different_goals_with_the_same_slug_both_survive(
        self, _isolated_library
    ) -> None:
        """slugify реже на 48 символа, така че различни цели с общ дълъг
        префикс се сблъскват. Втората не бива да презапише първата — точно
        случаят, който save_skill's own comment records as caught by hand."""
        long_prefix = "build a stdlib only utility that does something useful with"
        sm.save_skill(slug=long_prefix + " retries",
                      code=_CODE, goal=long_prefix + " retries")
        sm.save_skill(slug=long_prefix + " timeouts",
                      code=_CODE, goal=long_prefix + " timeouts")
        names = [s["name"] for s in _index(_isolated_library)]
        assert len(names) == 2, names
        assert len(set(names)) == 2, f"вторият запис е презаписал първия: {names}"
        md_files = sorted(p.name for p in _isolated_library.glob("*.md"))
        assert len(md_files) == 2, md_files

    def test_resaving_the_same_goal_deduplicates_instead_of_growing(
        self, _isolated_library
    ) -> None:
        """deep_verifier преверифицира умения; ако всяко минаване добавяше нов
        запис, библиотеката щеше да расте безкрайно от повторни проверки."""
        for _ in range(4):
            sm.save_skill(slug="същата цел", code=_CODE, goal="една и съща цел")
        assert len(_index(_isolated_library)) == 1, _index(_isolated_library)

    def test_concurrent_saves_do_not_drop_entries(self, _isolated_library) -> None:
        """_SAVE_LOCK съществува точно за това. Без него read-modify-write на
        индекса губи записи при паралелни мисии (parallel_forge стартира
        няколко наведнъж)."""
        def _save(i: int) -> None:
            sm.save_skill(slug=f"concurrent goal number {i}",
                          code=_CODE, goal=f"concurrent goal number {i}")

        threads = [threading.Thread(target=_save, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        entries = _index(_isolated_library)
        assert len(entries) == 12, f"загубени записи при конкурентност: {len(entries)}"
        assert len({s["name"] for s in entries}) == 12


class TestIndexAndDiskStayInSync:
    def test_every_index_entry_has_its_file(self, _isolated_library) -> None:
        for i in range(5):
            sm.save_skill(slug=f"goal {i}", code=_CODE, goal=f"goal number {i}")
        for entry in _index(_isolated_library):
            md = _isolated_library / f"{entry['name']}.md"
            assert md.is_file(), f"индексът сочи към липсващ файл: {md.name}"

    def test_every_file_has_its_index_entry(self, _isolated_library) -> None:
        for i in range(5):
            sm.save_skill(slug=f"goal {i}", code=_CODE, goal=f"goal number {i}")
        names = {s["name"] for s in _index(_isolated_library)}
        for md in _isolated_library.glob("*.md"):
            assert md.stem in names, f"файл без запис в индекса: {md.name}"

    def test_the_saved_code_round_trips_into_the_md(self, _isolated_library) -> None:
        """Чете се от самия .md, не през skill_loader: той резолвва по своите
        пътища и тук щеше да сочи към истинската библиотека, не към тази."""
        sm.save_skill(slug="round trip", code=_CODE, goal="round trip goal")
        name = _index(_isolated_library)[0]["name"]
        md = (_isolated_library / f"{name}.md").read_text(encoding="utf-8")
        assert "def f():" in md
        assert "```python" in md, "форматът трябва да остане този, който skill_loader чете"

    def test_a_corrupt_index_does_not_crash_the_next_save(
        self, _isolated_library
    ) -> None:
        (_isolated_library / "skills.json").write_text("{ не е json", encoding="utf-8")
        sm.save_skill(slug="после повредата", code=_CODE, goal="след повреден индекс")
        assert len(_index(_isolated_library)) == 1
        assert (_isolated_library / "skills.json.corrupt").is_file(), (
            "повреденият индекс е картата към умения, които още са на диска — "
            "запазва се, не се трие")

    def test_a_second_corruption_does_not_overwrite_the_first_rescue(
        self, _isolated_library
    ) -> None:
        """Ръбът, който обезсмисля самото запазване: след първа повреда
        индексът тръгва празен, и ако после се повреди И той, вторият (почти
        празен) не бива да презапише първия — там са старите умения."""
        (_isolated_library / "skills.json").write_text(
            '{"skills": [{"name": "ЦЕННО"}] повреден', encoding="utf-8")
        sm.save_skill(slug="първи", code=_CODE, goal="първа цел")

        (_isolated_library / "skills.json").write_text("{ пак повреда", encoding="utf-8")
        sm.save_skill(slug="втори", code=_CODE, goal="втора цел")

        first = (_isolated_library / "skills.json.corrupt").read_text(encoding="utf-8")
        assert "ЦЕННО" in first, "първото спасяване беше презаписано"
        assert (_isolated_library / "skills.json.corrupt.2").is_file()


class TestTheSlugCannotEscapeTheDirectory:
    @pytest.mark.parametrize("hostile", [
        "../../../etc/passwd",
        "..\\..\\windows\\system32",
        "a/b/c",
        "....//....//x",
        "/absolute/path",
        "name\x00truncated",
    ])
    def test_hostile_names_stay_inside(self, _isolated_library, hostile) -> None:
        sm.save_skill(slug=hostile, code=_CODE, goal=f"цел: {hostile}")
        written = list(_isolated_library.glob("**/*.md"))
        assert written, "нищо не е записано"
        for p in written:
            assert p.parent == _isolated_library, f"файл извън библиотеката: {p}"

    def test_slugify_never_returns_an_empty_or_dotted_name(self) -> None:
        for text in ("", "   ", "...", "///", "\x00", "..", "ЦЕЛ на кирилица"):
            s = sm.slugify(text)
            assert s, f"празен slug за {text!r}"
            assert "/" not in s and "\\" not in s
            assert s.strip(".") == s, f"slug от точки: {s!r}"


class TestCyrillicGoalsAreDistinguishable:
    """Проектът е на български и операторът пише целите на български, но
    slugify маха всичко извън [a-z0-9] — така всяка кирилска цел дава един и
    същ базов slug. Колизионната защита го спасява от презапис (виж тестовете
    по-горе), но името и тригерът остават нечитаеми.
    """

    def test_different_cyrillic_goals_do_not_overwrite_each_other(
        self, _isolated_library
    ) -> None:
        sm.save_skill(slug="извлечи данни от csv", code=_CODE,
                      goal="извлечи данни от csv файл")
        sm.save_skill(slug="изпрати отчет по поща", code=_CODE,
                      goal="изпрати отчет по поща")
        entries = _index(_isolated_library)
        assert len(entries) == 2, f"кирилска цел презаписа друга: {entries}"

    def test_slugify_itself_still_keeps_only_latin(self) -> None:
        """`slugify` нарочно си остава такава: смесените цели запазват
        латинското ("извлечи данни от csv" -> "csv"), а изцяло кирилската не
        оставя нищо и пада на фолбека.

        Това вече не е дупка. Операторът реши (2026-09-20) имената да се пазят
        на АНГЛИЙСКИ, не транслитерирани — затова `save_skill` взима името от
        кода на самото умение, когато целта не оставя нищо, а намирането на
        български минава по тригерите и описанието. Виж
        TestNamedInEnglishFoundInBulgarian по-долу."""
        assert sm.slugify("извлечи данни от csv") == "csv", "латиницата оцелява"
        assert sm.slugify("съвсем различна цел") == "skill"
        assert sm.slugify("напиши отчет") == "skill"


class TestIndexRecoveryIsNarrow:
    """Спасяването трябва да се задейства при ПОВРЕДА, не при всяка грешка.
    Прекалено широкият catch е също толкова опасен, колкото липсващият:
    премества здрав индекс настрани и следващият запис прави загубата трайна.
    """

    @pytest.mark.parametrize("payload", ['[]', '"низ"', 'null', '42',
                                         '{"skills": "не е списък"}'])
    def test_valid_json_of_the_wrong_shape_is_also_rescued(
        self, _isolated_library, payload
    ) -> None:
        """`[]` минава през json.loads, после `.get("skills")` гърми при всяко
        следващо извикване — същият провал, просто по друг път дотам."""
        (_isolated_library / "skills.json").write_text(payload, encoding="utf-8")
        assert sm.list_skills() == []
        sm.save_skill(slug="след грешна форма", code=_CODE, goal="цел")
        assert len(_index(_isolated_library)) == 1

    def test_a_transient_read_error_does_not_move_a_healthy_index(
        self, _isolated_library, monkeypatch
    ) -> None:
        """Windows дава PermissionError, когато редактор/антивирус държи
        файла. Ако това броеше за повреда, здравият индекс щеше да бъде
        преместен настрани и следващият запис щеше да направи загубата
        трайна — при положение че нищо не е било счупено."""
        sm.save_skill(slug="ценно умение", code=_CODE, goal="ценна цел")
        assert len(_index(_isolated_library)) == 1

        real_read = Path.read_text

        def _locked(self, *a, **kw):
            if self.name == "skills.json":
                raise PermissionError("файлът е зает от друг процес")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _locked)
        with pytest.raises(PermissionError):
            sm.list_skills()

        monkeypatch.undo()
        assert not (_isolated_library / "skills.json.corrupt").exists(), (
            "преходна грешка не бива да мести здрав индекс")
        assert len(_index(_isolated_library)) == 1, "умението трябва още да е в индекса"


class TestNamedInEnglishFoundInBulgarian:
    """Решение на оператора (2026-09-20): имената на уменията се пазят на
    английски, но българска заявка трябва да стига до точното умение.

    Двете заедно, защото поотделно всяко е безполезно: английско име, което
    не може да се намери на български, е умение, което не съществува за своя
    оператор; а намираемо умение с име `skill_4f1a2b` не казва нищо на човека,
    който гледа библиотеката.
    """

    _CYRILLIC_GOAL = "Изчисти временните файлове по график"
    _REAL_CODE = (
        "import os\n\n"
        "def cleanup_temp_files(root='/tmp'):\n"
        "    return [p for p in os.listdir(root) if p.endswith('.tmp')]\n\n"
        "assert isinstance(cleanup_temp_files('/tmp'), list)\n"
        "print('OK')\n"
    )

    def test_a_cyrillic_goal_is_named_from_the_code_not_from_a_hash(
        self, _isolated_library
    ) -> None:
        """`slugify` не оставя нищо от изцяло кирилска цел, тоест всички
        такива умения се казваха `skill` + хеш суфикс. Името обаче вече
        съществува в самото умение: моделите пишат идентификаторите на
        английски."""
        sm.save_skill(slug=self._CYRILLIC_GOAL, code=self._REAL_CODE,
                      goal=self._CYRILLIC_GOAL)
        names = [e["name"] for e in _index(_isolated_library)]
        assert names == ["cleanup_temp_files"], names

    def test_the_original_goal_is_kept_as_a_trigger(self, _isolated_library) -> None:
        """Името е английско, затова българската заявка съвпада само ако
        цялата цел е запазена като тригер."""
        sm.save_skill(slug=self._CYRILLIC_GOAL, code=self._REAL_CODE,
                      goal=self._CYRILLIC_GOAL)
        triggers = _index(_isolated_library)[0]["triggers"]
        assert self._CYRILLIC_GOAL in triggers, triggers
        assert "cleanup temp files" in triggers, triggers

    def test_a_bulgarian_query_finds_the_english_named_skill(
        self, _isolated_library, monkeypatch
    ) -> None:
        from genesis_agent import skill_loader as sl
        sm.save_skill(slug=self._CYRILLIC_GOAL, code=self._REAL_CODE,
                      goal=self._CYRILLIC_GOAL)
        monkeypatch.setattr(sl, "SKILLS_DIR", _isolated_library)
        monkeypatch.setattr(sl, "SKILLS_ROOT", _isolated_library.parent)
        monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", None)

        hits = sl.search_skills("изчисти временните файлове", top_n=3,
                                use_semantic=False)
        assert [h["name"] for h in hits] == ["cleanup_temp_files"], hits

    def test_an_english_goal_is_still_named_after_the_goal(
        self, _isolated_library
    ) -> None:
        """Правилото важи САМО когато целта не оставя нищо — иначе името би
        спряло да описва за какво е поискано умението."""
        sm.save_skill(slug="parse a csv report", code=self._REAL_CODE,
                      goal="parse a csv report")
        assert [e["name"] for e in _index(_isolated_library)] == ["parse_a_csv_report"]

    def test_uninformative_and_private_names_are_passed_over(self) -> None:
        code = ("def _helper():\n    pass\n\n"
                "def main():\n    pass\n\n"
                "def summarise_invoices():\n    pass\n")
        assert sm.english_name_from_code(code) == "summarise_invoices"

    def test_only_main_is_better_than_nothing(self) -> None:
        assert sm.english_name_from_code("def main():\n    pass\n") == "main"

    def test_code_that_does_not_parse_falls_back_instead_of_raising(
        self, _isolated_library
    ) -> None:
        assert sm.english_name_from_code("def broken(:\n") == ""
        sm.save_skill(slug=self._CYRILLIC_GOAL, code="x = 1\nprint('OK')\n",
                      goal=self._CYRILLIC_GOAL)
        assert [e["name"] for e in _index(_isolated_library)] == [sm.SLUG_FALLBACK]


class TestFrontmatterSurvivesRealText:
    """Frontmatter-ът се сглобяваше на ръка с единични кавички. Апостроф в
    целта ("don't repeat the user's work") дава невалиден YAML, а
    skill_loader лови YAMLError и продължава с ПРАЗНИ метаданни: умението се
    зарежда, но описанието и тригерите му изчезват и то не може да бъде
    намерено никога. Тихо, без грешка."""

    def _metadata(self, lib, name: str, monkeypatch) -> dict:
        from genesis_agent import skill_loader as sl
        monkeypatch.setattr(sl, "SKILLS_DIR", lib)
        monkeypatch.setattr(sl, "SKILLS_ROOT", lib.parent)
        monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", None)
        return sl.skill_view(name)["metadata"]

    def test_an_apostrophe_no_longer_erases_the_metadata(
        self, _isolated_library, monkeypatch
    ) -> None:
        goal = "don't repeat the user's work"
        sm.save_skill(slug="apostrophe test", code=_CODE, goal=goal)
        md = self._metadata(_isolated_library, "apostrophe_test", monkeypatch)
        assert md["description"] == goal
        assert goal in md["triggers"]

    def test_a_colon_and_a_hash_survive_too(
        self, _isolated_library, monkeypatch
    ) -> None:
        goal = "fix: bug #42 in the parser"
        sm.save_skill(slug="colon test", code=_CODE, goal=goal)
        md = self._metadata(_isolated_library, "colon_test", monkeypatch)
        assert md["description"] == goal

    def test_cyrillic_is_written_as_text_not_as_escapes(
        self, _isolated_library
    ) -> None:
        """`allow_unicode=False` би записало \\u0418... — валиден YAML, но
        файлът става нечитаем за човека, който отваря умението."""
        goal = "Изчисти временните файлове"
        sm.save_skill(slug="cyrillic desc", code=_CODE, goal=goal)
        text = (_isolated_library / "cyrillic_desc.md").read_text(encoding="utf-8")
        assert goal in text
        assert "\\u" not in text
