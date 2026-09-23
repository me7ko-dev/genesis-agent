"""Доставените умения стигат ли до вече съществуваща инсталация.

Измерено на живо преди поправката, с инсталиран пакет и чист HOME:

    прясна инсталация (няма ~/.genesis)          → 20 умения
    същата версия, но ~/.genesis/skills с 1      → 1 умение

Тоест `pip install --force` обновяваше КОДА, а библиотеката оставаше отпреди
— завинаги, при всяко следващо обновяване. Никакво съобщение, никаква грешка;
операторът тества нова версия със стари умения и няма как да разбере.

Правилото е „само добавяй": версията на оператора за същото име винаги печели,
защото той може да я е променил.
"""
from __future__ import annotations

import json

import pytest

from genesis_agent.config import seed_user_skills


def _shipped(tmp_path, names=("alpha", "beta")):
    d = tmp_path / "shipped"
    d.mkdir()
    for n in names:
        (d / f"{n}.md").write_text(f"---\nname: {n}\n---\n\n```python\nprint('{n}')\n```\n",
                                   encoding="utf-8")
    (d / "skills.json").write_text(json.dumps({
        "version": "1.0",
        "skills": [{"name": n, "file_path": f"skills/{n}.md", "description": n,
                    "triggers": [n], "verified": True} for n in names]
    }, ensure_ascii=False), encoding="utf-8")
    return d


def _index(user_dir):
    return json.loads((user_dir / "skills.json").read_text(encoding="utf-8"))


def _names(user_dir):
    return sorted(s["name"] for s in _index(user_dir)["skills"])


class TestAFreshInstallGetsEverything:
    def test_all_shipped_files_land(self, tmp_path) -> None:
        user = tmp_path / "home" / "skills"
        added = seed_user_skills(_shipped(tmp_path), user)
        assert added == 2
        assert sorted(p.stem for p in user.glob("*.md")) == ["alpha", "beta"]

    def test_the_index_is_written_too(self, tmp_path) -> None:
        """Файл без запис в индекса е умение, което не може да бъде намерено."""
        user = tmp_path / "home" / "skills"
        seed_user_skills(_shipped(tmp_path), user)
        assert _names(user) == ["alpha", "beta"]


class TestAnExistingInstallIsToppedUp:
    """Това е случаят, който не работеше изобщо."""

    def _existing(self, tmp_path):
        user = tmp_path / "home" / "skills"
        user.mkdir(parents=True)
        (user / "my_own.md").write_text("---\nname: my_own\n---\n\n```python\npass\n```\n",
                                        encoding="utf-8")
        (user / "skills.json").write_text(json.dumps({
            "version": "1.0",
            "skills": [{"name": "my_own", "file_path": "skills/my_own.md",
                        "description": "мое", "triggers": ["my own"], "verified": True}]
        }, ensure_ascii=False), encoding="utf-8")
        return user

    def test_new_shipped_skills_arrive(self, tmp_path) -> None:
        user = self._existing(tmp_path)
        assert seed_user_skills(_shipped(tmp_path), user) == 2
        assert _names(user) == ["alpha", "beta", "my_own"]

    def test_the_operators_own_skill_survives(self, tmp_path) -> None:
        user = self._existing(tmp_path)
        seed_user_skills(_shipped(tmp_path), user)
        assert (user / "my_own.md").exists()

    def test_a_modified_shipped_skill_is_not_overwritten(self, tmp_path) -> None:
        """Операторът може да е поправил доставено умение. Неговата версия
        печели — обновяването не е право да трие чужда работа."""
        user = self._existing(tmp_path)
        mine = user / "alpha.md"
        mine.write_text("моята версия\n", encoding="utf-8")
        seed_user_skills(_shipped(tmp_path), user)
        assert mine.read_text(encoding="utf-8") == "моята версия\n"

    def test_only_the_missing_ones_are_counted(self, tmp_path) -> None:
        user = self._existing(tmp_path)
        (user / "alpha.md").write_text("вече е тук\n", encoding="utf-8")
        assert seed_user_skills(_shipped(tmp_path), user) == 1


class TestItCanBeRunOnEveryStart:
    def test_running_twice_adds_nothing_the_second_time(self, tmp_path) -> None:
        user = tmp_path / "home" / "skills"
        shipped = _shipped(tmp_path)
        seed_user_skills(shipped, user)
        assert seed_user_skills(shipped, user) == 0

    def test_no_duplicate_index_entries(self, tmp_path) -> None:
        user = tmp_path / "home" / "skills"
        shipped = _shipped(tmp_path)
        seed_user_skills(shipped, user)
        seed_user_skills(shipped, user)
        names = [s["name"] for s in _index(user)["skills"]]
        assert len(names) == len(set(names)), names


class TestItSurvivesABrokenIndex:
    def test_a_corrupt_index_does_not_stop_the_copy(self, tmp_path) -> None:
        """Счупен индекс е реално състояние (прекъснат запис). Той не бива да
        значи „никакви умения оттук нататък"."""
        user = tmp_path / "home" / "skills"
        user.mkdir(parents=True)
        (user / "skills.json").write_text("{това не е json", encoding="utf-8")
        assert seed_user_skills(_shipped(tmp_path), user) == 2
        assert _names(user) == ["alpha", "beta"]

    def test_a_missing_shipped_index_still_copies_files(self, tmp_path) -> None:
        shipped = _shipped(tmp_path)
        (shipped / "skills.json").unlink()
        user = tmp_path / "home" / "skills"
        assert seed_user_skills(shipped, user) == 2
        assert sorted(p.stem for p in user.glob("*.md")) == ["alpha", "beta"]


class TestTheIndexIsNeverLeftHalfWritten:
    def test_no_temp_file_is_left_behind(self, tmp_path) -> None:
        user = tmp_path / "home" / "skills"
        seed_user_skills(_shipped(tmp_path), user)
        assert not list(user.glob("*.tmp")), "временният файл трябва да е преместен"


@pytest.mark.parametrize("shipped_first", [True, False])
def test_order_does_not_change_the_outcome(tmp_path, shipped_first) -> None:
    """Дали операторът си е записал умение преди или след обновяването, не
    бива да мени резултата."""
    user = tmp_path / "home" / "skills"
    shipped = _shipped(tmp_path)
    if shipped_first:
        seed_user_skills(shipped, user)
        (user / "mine.md").write_text("x", encoding="utf-8")
    else:
        user.mkdir(parents=True)
        (user / "mine.md").write_text("x", encoding="utf-8")
        seed_user_skills(shipped, user)
    assert {p.stem for p in user.glob("*.md")} == {"alpha", "beta", "mine"}
