"""genesis_agent.reflection — „учи се от грешките си“. Нула покритие преди
този файл, при положение че `lessons_for_prompt()` влиза в системния промпт на
всяка мисия и всяка оркестрация.

Измерено на реална база от 1967 епизода: прозорецът беше последните N ЗАПИСА,
а в последните 60 записа имаше 7 мисии — останалите бяха единични извиквания
на инструменти (`READ_FILE`, `WRITE_FILE`). Тоест функцията, която съществува
да намира ПОВТАРЯЩИ се провали, гледаше проба, десет пъти по-малка от
поисканата, и мълчаливо връщаше „няма уроци“.

Епизодичната база тук е изолирана от conftest (виж tests/test_state_isolation.py).
"""
from __future__ import annotations

from genesis_agent import episodic_memory as em
from genesis_agent import reflection as r


def _mission(goal: str, *, ok: bool, lesson: str = "") -> None:
    em.record_episode(
        goal=goal, outcome="success" if ok else "failed", skill_path="test",
        lessons_learned=[lesson] if lesson else None,
        tags=["mission", "success" if ok else "failure"],
    )


def _tool_call(goal: str) -> None:
    em.record_episode(goal=goal, outcome="прочетен", skill_path="test",
                      tags=["tool", "read_file"])


class TestTheWindowCountsMissions:
    def test_tool_calls_do_not_crowd_out_the_missions(self) -> None:
        """Точната форма на реалната база: шепа мисии, погребани под стотици
        извиквания на инструменти."""
        for i in range(5):
            _mission(f"мисия {i}", ok=False,
                     lesson="ModuleNotFoundError: No module named 'pandas'")
        for i in range(200):
            _tool_call(f"READ_FILE /tmp/{i}")

        lessons = r.distill_lessons(last_n=60)
        assert lessons, "провалите са в базата, но прозорецът не ги вижда"
        assert "Липсващ пакет" in lessons[0]

    def test_the_limit_means_that_many_missions(self) -> None:
        for i in range(10):
            _mission(f"стара мисия {i}", ok=True)
            _tool_call(f"READ_FILE /tmp/old{i}")
        assert len(r._recent_missions(10)) == 10
        assert all("mission" in e["tags"] for e in r._recent_missions(10))

    def test_reuse_rate_is_measured_over_missions_too(self) -> None:
        for i in range(300):
            _tool_call(f"READ_FILE /tmp/{i}")
        for i in range(4):
            em.record_episode(goal=f"композирана {i}", outcome="success",
                              skill_path="test", tags=["mission", "success", "composed"])
        for i in range(4):
            _mission(f"от нула {i}", ok=True)
        assert r.reuse_rate(last_n=100) == 0.5

    def test_no_missions_at_all_reports_none_not_zero(self) -> None:
        """Нула значи „нищо не преизползва“; None значи „няма какво да се
        мери“. Смесването им прави пресен инсталация да изглежда като провал."""
        for i in range(5):
            _tool_call(f"READ_FILE /tmp/{i}")
        assert r.reuse_rate(last_n=50) is None


class TestWhatReachesThePrompt:
    def test_the_most_common_failure_leads(self) -> None:
        for _ in range(4):
            _mission("а", ok=False, lesson="SyntaxError: invalid syntax")
        for _ in range(2):
            _mission("б", ok=False, lesson="Timeout after 30s")
        lessons = r.distill_lessons(last_n=60, top=4)
        assert "Синтактични" in lessons[0], lessons

    def test_unknown_error_never_reaches_the_prompt(self) -> None:
        """„не повтаряй: Unknown Error“ не значи нищо за модела и краде един
        от малкото слотове от истински съвет."""
        for _ in range(9):
            _mission("нещо", ok=False, lesson="съвсем непознат текст")
        _mission("познат", ok=False, lesson="ModuleNotFoundError: no module named x")
        lessons = r.distill_lessons(last_n=60)
        assert all("Unknown" not in les for les in lessons), lessons
        assert any("Липсващ пакет" in les for les in lessons), lessons

    def test_nothing_to_say_means_an_empty_string_not_a_header(self) -> None:
        """Празна секция „УРОЦИ ОТ МИНАЛИ ГРЕШКИ“ е токени всяка заявка за
        нула информация."""
        _mission("всичко мина", ok=True)
        assert r.lessons_for_prompt(last_n=60) == ""

    def test_successful_missions_contribute_no_lessons(self) -> None:
        for _ in range(5):
            _mission("успешна", ok=True, lesson="ModuleNotFoundError някъде")
        assert r.distill_lessons(last_n=60) == []


class TestItNeverBreaksAMission:
    def test_a_broken_memory_layer_returns_empty_instead_of_raising(self, monkeypatch) -> None:
        def _boom():
            raise RuntimeError("базата е заключена")
        monkeypatch.setattr(em, "_fetch_all_episodes", _boom)
        assert r.distill_lessons() == []
        assert r.lessons_for_prompt() == ""
        assert r.reuse_rate() is None

    def test_an_episode_without_tags_or_lessons_is_survivable(self) -> None:
        em.record_episode(goal="без тагове", outcome="failed", skill_path="test")
        assert isinstance(r.distill_lessons(last_n=60), list)
