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


@pytest.fixture(autouse=True)
def _no_real_compaction(monkeypatch):
    monkeypatch.setattr(
        "genesis_agent.brain.Brain.compact_chat_history",
        staticmethod(lambda messages, threshold=16, keep_recent=10: messages),
    )
    yield


@pytest.fixture
def a_real_skill() -> str:
    """Кое да е умение от библиотеката — името нарочно не е заковано, за да не
    пада тестът, когато библиотеката се преименува или пренареди."""
    index = load_skills_index()
    if not index:
        pytest.skip("библиотеката с умения е празна в тази среда")
    return min(index)


class _ScriptedModel:
    """Модел, който казва точно каквото му е написано, в този ред."""

    def __init__(self, replies) -> None:
        self._replies = list(replies)
        self.skills = gs
        self.wm = None

    def complete(self, messages):
        if not self._replies:
            return ("готово", None, "fake", "scripted")
        return (self._replies.pop(0), None, "fake", "scripted")

    def remember(self, role, content) -> None:
        pass


def _run(core, user_text: str):
    executed: list[str] = []
    messages = ac.run_tool_loop(
        core, [{"role": "user", "content": user_text}],
        on_assistant=lambda t, p, m: None,
        on_tool_result=lambda name, result, extra: executed.append(result),
    )
    return executed, messages


class TestBareTagReachesTheSandbox:
    def test_the_short_form_a_model_writes_actually_runs_the_skill(
        self, a_real_skill, tmp_path
    ) -> None:
        gs.set_workspace(tmp_path)
        core = _ScriptedModel([f"Ще ползвам умението.\n[USE_SKILL: {a_real_skill}]",
                               "Готово."])
        executed, _ = _run(core, "използвай това умение")

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
        core = _ScriptedModel([f"[USE_SKILL: {a_real_skill}]", "Готово."])
        _, messages = _run(core, "давай")

        assert any("Достъпни:" in str(m.get("content", "")) for m in messages)

    def test_the_closed_form_still_reaches_the_sandbox_too(
        self, a_real_skill, tmp_path
    ) -> None:
        gs.set_workspace(tmp_path)
        core = _ScriptedModel([f"[USE_SKILL: {a_real_skill}]\npass\n[END_USE_SKILL]",
                               "Готово."])
        executed, _ = _run(core, "давай")

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
        core = _ScriptedModel(["[USE_SKILL: no_such_skill_anywhere]"] * 30)
        executed, _ = _run(core, "направи нещо")

        from genesis_agent.repeat_guard import STOP_AT
        assert len(executed) == STOP_AT
        assert "Няма достатъчно близко умение" in executed[0]
