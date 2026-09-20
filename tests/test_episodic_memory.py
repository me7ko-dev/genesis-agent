"""genesis_agent.episodic_memory — `summarize_sessions` пише текст, който
отива в СИСТЕМНИЯ промпт, тоест се плаща на всяка заявка до модела.

Измерено на живо преди тези тестове (1625 епизода в реалната база):

    555 × ['tool', 'read_file']      333 × ['mission', 'success']
    222 × ['tool', 'write_file']     222 × ['mission', 'failure']

„Последните 6 епизода“ означаваше шест реда от типа
`READ_FILE /tmp/pytest-of-root/.../note.txt -> прочетен` — включително от
собствения тестов пакет. Нито един не казваше нищо за работата на оператора.
"""
from __future__ import annotations

import pytest

from genesis_agent import episodic_memory as em


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """`_get_connection` чете модулния DB_PATH — пренасочваме го към файл за
    изхвърляне, за да не пипнем реалната episodes.db."""
    monkeypatch.setattr(em, "DB_PATH", tmp_path / "episodes.db")
    em._init_db()
    yield


def _mission(goal: str, outcome: str = "success") -> None:
    em.record_episode(goal=goal, outcome=outcome, skill_path="test",
                      tags=["mission", outcome])


def _tool_call(goal: str) -> None:
    em.record_episode(goal=goal, outcome="прочетен", skill_path="test",
                      tags=["tool", "read_file"])


class TestKindsFilter:
    def test_without_kinds_everything_is_returned_as_before(self) -> None:
        _tool_call("READ_FILE /tmp/x.txt")
        _mission("мигрирай билинга")
        text = em.summarize_sessions(last_n=10)
        assert "READ_FILE" in text
        assert "мигрирай билинга" in text

    def test_missions_only_drops_the_tool_log(self) -> None:
        for i in range(10):
            _tool_call(f"READ_FILE /tmp/pytest-of-root/case{i}/note.txt")
        _mission("мигрирай билинга към новото API")
        text = em.summarize_sessions(last_n=6, kinds=("mission",))
        assert "мигрирай билинга" in text
        assert "READ_FILE" not in text, text

    def test_the_limit_counts_matching_episodes_not_scanned_rows(self) -> None:
        """Иначе филтърът е безполезен: 6 реда, от които 6 са извиквания на
        инструмент, връщат нула мисии, макар мисии да има."""
        for i in range(20):
            _tool_call(f"READ_FILE /tmp/{i}")
        for i in range(3):
            _mission(f"мисия {i}")
        text = em.summarize_sessions(last_n=6, kinds=("mission",))
        assert all(f"мисия {i}" in text for i in range(3)), text

    def test_failed_missions_are_kept_that_is_the_useful_half(self) -> None:
        em.record_episode(goal="деплой на staging", outcome="failed",
                          skill_path="test", tags=["mission", "failure"],
                          lessons_learned=["липсва DATABASE_URL"])
        text = em.summarize_sessions(last_n=5, kinds=("mission",))
        assert "деплой на staging" in text
        assert "липсва DATABASE_URL" in text

    def test_no_matching_episodes_says_so_instead_of_raising(self) -> None:
        _tool_call("READ_FILE /tmp/x")
        assert em.summarize_sessions(last_n=5, kinds=("mission",)) == "Няма записани епизоди."


class TestSummaryStaysASummary:
    def test_a_long_outcome_is_not_reproduced_in_full(self) -> None:
        """Изходът на инструмент се записва до 2000 символа. Ред от 2000
        символа в системния промпт се плаща на всеки рунд до края на сесията."""
        em.record_episode(goal="пусни тестовете", outcome="x" * 2000,
                          skill_path="test", tags=["mission", "success"])
        text = em.summarize_sessions(last_n=1, kinds=("mission",))
        assert len(text) < 400, len(text)
        assert "…" in text

    def test_newlines_inside_a_field_cannot_break_the_line_format(self) -> None:
        """Един епизод е един ред. Многоредов изход иначе изглежда като
        няколко епизода и моделът брои грешно какво се е случило."""
        em.record_episode(goal="цел\nс нов ред", outcome="ok\nok\nok",
                          skill_path="test", tags=["mission", "success"])
        text = em.summarize_sessions(last_n=1, kinds=("mission",))
        assert len(text.splitlines()) == 2, text   # заглавие + един епизод

    def test_a_short_entry_is_left_exactly_as_it_is(self) -> None:
        _mission("кратка цел")
        text = em.summarize_sessions(last_n=1, kinds=("mission",))
        assert "кратка цел ->" in text
        assert "…" not in text
