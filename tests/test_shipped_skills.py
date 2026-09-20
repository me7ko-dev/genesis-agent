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
