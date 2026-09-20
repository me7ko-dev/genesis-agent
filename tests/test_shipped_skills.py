"""Уменията, които се доставят с репото — цялост на самите файлове.

Тук се хвана истинският случай на един тих бъг: `project_checks_detect_and_run`
има описание „Detect and run a project's OWN checks…", а frontmatter-ът се
сглобяваше на ръка с единични кавички. Апострофът в „project's" правеше YAML-а
невалиден, `skill_loader` лови YAMLError и продължава с ПРАЗНИ метаданни —
умението се зарежда, но описанието и тригерите му ги няма. Индексът
(skills.json) е JSON и си беше наред, затова търсенето работеше и нищо не
изглеждаше счупено.

Затова проверката е върху ФАЙЛОВЕТЕ, не върху индекса: те са преносимата
форма на умението, тази, която се споделя и от която индексът може да бъде
възстановен.
"""
from __future__ import annotations

import json
import re

import pytest
import yaml

from genesis_agent.config import SKILLS_DIR

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_SKILL_FILES = sorted(SKILLS_DIR.glob("*.md"))


def _ids(paths):
    return [p.stem for p in paths]


@pytest.mark.parametrize("md", _SKILL_FILES, ids=_ids(_SKILL_FILES))
class TestEverySkillFileIsReadable:
    def test_the_frontmatter_parses(self, md) -> None:
        match = _FRONTMATTER_RE.match(md.read_text(encoding="utf-8"))
        assert match, f"{md.name}: няма frontmatter"
        yaml.safe_load(match.group(1))   # хвърля при невалиден YAML

    def test_it_carries_a_description_and_triggers(self, md) -> None:
        """Празни метаданни не са грешка за `skill_view` — то ги поглъща и
        продължава. Умението остава изпълнимо и невидимо за търсенето."""
        meta = yaml.safe_load(_FRONTMATTER_RE.match(
            md.read_text(encoding="utf-8")).group(1)) or {}
        assert meta.get("description"), f"{md.name}: няма описание"
        assert meta.get("triggers"), f"{md.name}: няма тригери"

    def test_it_has_a_python_block(self, md) -> None:
        assert "```python" in md.read_text(encoding="utf-8"), f"{md.name}: няма код"


class TestTheFilesAndTheIndexAgree:
    def test_every_indexed_skill_has_its_file(self) -> None:
        index = json.loads((SKILLS_DIR / "skills.json").read_text(encoding="utf-8"))
        missing = [s["name"] for s in index["skills"]
                   if not (SKILLS_DIR / f"{s['name']}.md").is_file()]
        assert not missing, f"в индекса, но без файл: {missing}"

    def test_the_triggers_match(self) -> None:
        """Двете места се пишат поотделно; разминат ли се, намирането зависи
        от това кое от двете е прочетено."""
        index = json.loads((SKILLS_DIR / "skills.json").read_text(encoding="utf-8"))
        for entry in index["skills"]:
            md = SKILLS_DIR / f"{entry['name']}.md"
            if not md.is_file():
                continue
            meta = yaml.safe_load(_FRONTMATTER_RE.match(
                md.read_text(encoding="utf-8")).group(1)) or {}
            assert set(meta.get("triggers") or []) == set(entry.get("triggers") or []), \
                entry["name"]


class TestTheWorkflowSkillsAnswerInBulgarian:
    """Операторът пише на български, а тези четири умения са най-полезните в
    библиотеката — останалите са учебни упражнения. Имената им са английски
    (решение на оператора), затова намирането минава през тригерите."""

    @pytest.mark.parametrize("query,expected", [
        ("пусни проверките на проекта", "project_checks_detect_and_run"),
        ("кои тестове да пусна за промените", "changed_surface_which_tests_to_run"),
        ("коя е първата истинска грешка", "first_real_failure_not_the_cascade"),
        ("как да редактирам файл безопасно", "safe_edit_probe_anchor_uniqueness"),
    ])
    def test_a_bulgarian_request_reaches_the_right_one(self, query, expected) -> None:
        from genesis_agent import skill_loader as sl
        sl._SKILLS_INDEX_CACHE = None
        resolved, _ = sl.resolve_skill(query)
        assert resolved == expected, f"{query!r} → {resolved}"

    def test_an_unrelated_request_still_reaches_nothing(self) -> None:
        from genesis_agent import skill_loader as sl
        sl._SKILLS_INDEX_CACHE = None
        resolved, _ = sl.resolve_skill("нещо съвсем несвързано за марсианци")
        assert resolved is None


class TestNoShippedSkillCarriesAPrivateSignature:
    """Подпис в доставяния индекс е направен с ЛИЧНИЯ ключ на този, който го е
    записал. На всяка друга машина той не съвпада — а `skill_view` не може да
    различи „подписано от друг" от „подправено" и отказва умението, обвинявайки
    потребителя в намеса.

    Измерено: с генериран собствен ключ 11 доставени умения станаха незаредими
    с точно това съобщение. Файловете в репото се пазят от git; подписът пази
    другото — локално записаното, след записването му.
    """

    def test_the_index_has_no_signatures(self) -> None:
        index = json.loads((SKILLS_DIR / "skills.json").read_text(encoding="utf-8"))
        signed = [s["name"] for s in index["skills"] if s.get("signature")]
        assert not signed, (
            "доставени умения с чужд подпис (махни полето 'signature'): " + ", ".join(signed))

    def _save_into(self, monkeypatch, tmp_path, skills_dir, package_dir):
        from genesis_agent import skills_manager as sm
        skills_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(sm, "SKILLS_DIR", skills_dir)
        monkeypatch.setattr(sm, "SKILLS_ROOT", skills_dir.parent)
        monkeypatch.setattr(sm, "PACKAGE_DIR", package_dir)
        sm.save_skill(slug="signing probe", goal="Signing probe skill.",
                      code="def probe():\n    return 1\n\nassert probe() == 1\nprint('OK')\n")
        index = json.loads((skills_dir / "skills.json").read_text(encoding="utf-8"))
        assert len(index["skills"]) == 1, index["skills"]
        return index["skills"][0]

    def test_saving_into_the_shipped_dir_does_not_sign(self, monkeypatch, tmp_path) -> None:
        """Пазачът е в кода, не в дисциплината: иначе следващото умение,
        записано по време на разработка, връща проблема тихо."""
        package_dir = tmp_path / "pkg"
        entry = self._save_into(monkeypatch, tmp_path, package_dir / "skills", package_dir)
        assert not entry.get("signature"), (
            "умение в доставяната папка не бива да носи личен подпис")

    def test_saving_into_the_operators_own_dir_still_signs(self, monkeypatch, tmp_path) -> None:
        """Поправката не бива да изключи подписването изобщо — то пази точно
        локално записаните умения от промяна след записа."""
        pytest.importorskip("cryptography")
        monkeypatch.setenv("GENESIS_KEY_DIR", str(tmp_path / "keys"))
        entry = self._save_into(monkeypatch, tmp_path, tmp_path / "home" / "skills",
                                tmp_path / "pkg")
        assert entry.get("signature"), "локалното умение трябва да остане подписано"
