"""genesis_agent.trigger_engine — пътят „съществуващо умение без обръщение
към модел“. Нула покритие преди този файл.

Държеше собствено копие на токенизацията (`[a-z0-9_]+`, само ASCII), тоест
заявка на кирилица даваше нула думи и прагът от 2 съвпадения не можеше да
бъде достигнат никога. Сега ползва `skill_loader._keywords` — една функция,
една поправка следващия път.

Прагът е това, което пази от грешно задействане: умение, което бъде
задействано по една случайна дума, е по-лошо от никакво умение, защото
отговорът изглежда уверен.
"""
from __future__ import annotations

import json

import pytest

from genesis_agent import skill_loader as sl
from genesis_agent.trigger_engine import TriggerEngine

SKILLS = {"skills": [
    {"name": "exponential_backoff_retry", "file_path": "skills/a.md",
     "category": "autonomous", "description": "Retry with exponential backoff",
     "triggers": ["exponential backoff retry"]},
    {"name": "obrabotka_na_otcheti", "file_path": "skills/b.md",
     "category": "autonomous", "description": "Обработка на месечни отчети",
     "triggers": ["обработка месечни отчети"]},
]}


@pytest.fixture(autouse=True)
def _index(tmp_path, monkeypatch):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "skills.json").write_text(
        json.dumps(SKILLS, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sl, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(sl, "SKILLS_ROOT", tmp_path)
    monkeypatch.setattr(sl, "_SKILLS_INDEX_CACHE", None)
    yield


class TestMatching:
    def test_an_english_query_above_the_threshold_matches(self) -> None:
        hit = TriggerEngine().match("exponential backoff for API retry")
        assert hit is not None
        assert hit["name"] == "exponential_backoff_retry"

    def test_a_cyrillic_query_can_reach_the_threshold_at_all(self) -> None:
        """Преди поправката броят съвпаднали думи беше 0 по конструкция."""
        hit = TriggerEngine().match("обработка на месечни отчети")
        assert hit is not None, "кирилицата не се броеше изобщо"
        assert hit["name"] == "obrabotka_na_otcheti"

    def test_one_incidental_word_is_not_enough(self) -> None:
        """Прагът съществува точно за това: задействано по случайност умение
        дава уверен, но грешен отговор."""
        assert TriggerEngine().match("retry") is None

    def test_an_unrelated_query_matches_nothing(self) -> None:
        assert TriggerEngine().match("нещо напълно случайно за марсианци") is None

    def test_the_threshold_is_configurable(self) -> None:
        assert TriggerEngine(threshold=1).match("retry") is not None


class TestMatchAndRun:
    def test_it_describes_the_skill_instead_of_executing_it(self, monkeypatch) -> None:
        """Документираното поведение: връща описание за потвърждение, никога
        не изпълнява кода само."""
        monkeypatch.setattr("genesis_agent.trigger_engine.skill_view",
                            lambda name: {"code": "def retry():\n    pass\n"})
        text = TriggerEngine().match_and_run("exponential backoff retry helper")
        assert "exponential_backoff_retry" in text
        assert "def retry()" in text

    def test_an_unloadable_skill_still_returns_a_description(self, monkeypatch) -> None:
        def _boom(_name):
            raise RuntimeError("файлът липсва")
        monkeypatch.setattr("genesis_agent.trigger_engine.skill_view", _boom)
        text = TriggerEngine().match_and_run("exponential backoff retry helper")
        assert text is not None
        assert "не може да се зареди" in text

    def test_no_match_returns_none_so_the_caller_falls_back_to_the_model(self) -> None:
        assert TriggerEngine().match_and_run("марсианци") is None


class TestListTriggers:
    def test_it_lists_every_indexed_skill(self) -> None:
        names = {t["name"] for t in TriggerEngine().list_triggers()}
        assert names == {"exponential_backoff_retry", "obrabotka_na_otcheti"}
