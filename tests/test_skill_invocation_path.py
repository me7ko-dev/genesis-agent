"""Целият път от отговора на модела до изпълнено умение, в едно парче.

Всяко звено поотделно си има тестове — парсерът в test_genesis_skills.py,
цикълът в test_agent_core.py, зареждането в test_skill_loader.py — и всички
бяха зелени, докато пътят между тях не работеше: `[USE_SKILL: име]` без
затварящ таг не съвпадаше с нито един парсер, така че отговорът ИЗГЛЕЖДАШЕ
като извикване на умение и не изпълняваше нищо. Точно такава дупка не се
вижда от тест на звено.

Затова тук нищо не се подменя освен модела: истински парсер, истински
skill_loader, истински sandbox, истинско умение от библиотеката.
"""
from __future__ import annotations

import pytest

import genesis_agent.agent_core as ac
import genesis_skills as gs
from genesis_agent.skill_loader import load_skills_index


@pytest.fixture
def a_real_skill() -> str:
    """Кое да е умение от библиотеката — името нарочно не е заковано, за да не
    пада тестът, когато библиотеката се преименува или пренареди."""
    index = load_skills_index()
    if not index:
        pytest.skip("библиотеката с умения е празна в тази среда")
    return min(index)


class _Shown(ac.TurnUI):
    """Какво човекът е видял като изпълнено."""

    def __init__(self) -> None:
        self.executed: list[str] = []

    def tool(self, name: str, result: str) -> None:
        self.executed.append(result)


def _run(replies: list[str], user_text: str):
    """Модел, който казва точно каквото му е написано, в този ред."""
    script = list(replies)
    ui = _Shown()
    messages = [{"role": "user", "content": user_text}]
    ac.run_tool_loop(messages, user_text, ui,
                     lambda msgs: (script.pop(0) if script else "готово", None))
    return ui.executed, messages


class TestBareTagReachesTheSandbox:
    def test_the_short_form_a_model_writes_actually_runs_the_skill(
        self, a_real_skill, tmp_path
    ) -> None:
        gs.set_workspace(tmp_path)
        executed, _ = _run([f"Ще ползвам умението.\n[USE_SKILL: {a_real_skill}]", "Готово."],
                           "използвай това умение")

        assert len(executed) == 1, "умението не беше извикано изобщо"
        # "Достъпни:" идва от skill_loader._extract_signatures, тоест кодът
        # наистина е бил зареден и подаден на sandbox-а, не само разпознат.
        assert "Достъпни:" in executed[0]
        assert a_real_skill in executed[0]

    def test_the_result_reaches_the_history_the_model_sees_next_round(
        self, a_real_skill, tmp_path
    ) -> None:
        """Изпълнено, но невидимо за следващия рунд, е същото като неизпълнено.

        Търси се "Достъпни:" — ред, който се ражда чак в
        skill_loader._extract_signatures, тоест съществува само ако кодът е бил
        реално зареден. Името на умението не става за проверка: то е в
        репликата на модела, значи е в историята и когато нищо не е тръгнало.
        """
        gs.set_workspace(tmp_path)
        _, messages = _run([f"[USE_SKILL: {a_real_skill}]", "Готово."], "давай")

        assert any("Достъпни:" in str(m.get("content", "")) for m in messages)

    def test_the_closed_form_still_reaches_the_sandbox_too(
        self, a_real_skill, tmp_path
    ) -> None:
        gs.set_workspace(tmp_path)
        executed, _ = _run([f"[USE_SKILL: {a_real_skill}]\npass\n[END_USE_SKILL]", "Готово."],
                           "давай")

        assert len(executed) == 1
        assert "Достъпни:" in executed[0]


class TestAMissingSkillDoesNotSpin:
    def test_an_unknown_name_answers_once_and_does_not_burn_the_budget(
        self, tmp_path
    ) -> None:
        """Заявка, за която няма умение, беше най-скъпият случай: тагът не се
        разпознаваше, цикълът отговаряше "сбъркал си синтаксиса", моделът
        пишеше същото — и рундовете горяха. Сега умението се търси, отговорът
        е ясен, и повторението спира хода."""
        gs.set_workspace(tmp_path)
        executed, _ = _run(["[USE_SKILL: no_such_skill_anywhere]"] * 30, "направи нещо")

        from genesis_agent.repeat_guard import STOP_AT
        assert len(executed) == STOP_AT
        assert "Няма достатъчно близко умение" in executed[0]
