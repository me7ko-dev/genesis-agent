"""genesis_agent.repeat_guard — въртене на място vs. напредък.

Предпазителят решава кога един ход е спрял да носи нещо ново. Границата
трябва да е точно там, където е: цикъл се спира, напредък — никога. Тестовете
долу държат и двете страни, защото фалшивото спиране е по-скъпо от пропуснат
цикъл (реже истинска работа по средата и я прави недоверчива).
"""
from __future__ import annotations

from genesis_agent.repeat_guard import STOP_AT, WARN_AT, RepeatGuard


class TestSpinningIsCaught:
    def test_identical_call_and_result_stops_the_turn(self) -> None:
        g = RepeatGuard()
        args = {"name_or_query": "foo"}
        assert g.observe("USE_SKILL", args, "няма такова умение").stop is False
        assert g.observe("USE_SKILL", args, "няма такова умение").stop is False
        v = g.observe("USE_SKILL", args, "няма такова умение")
        assert v.stop is True
        assert v.repeats == STOP_AT

    def test_second_identical_call_warns_without_stopping(self) -> None:
        """Моделът получава шанс сам да смени подхода, преди ходът да спре."""
        g = RepeatGuard()
        g.observe("RUN_CMD", {"command": "ls"}, "изход")
        v = g.observe("RUN_CMD", {"command": "ls"}, "изход")
        assert v.repeats == WARN_AT
        assert v.stop is False
        assert v.note

    def test_the_note_names_the_tool_that_is_spinning(self) -> None:
        g = RepeatGuard()
        for _ in range(STOP_AT):
            v = g.observe("USE_SKILL", {"q": "x"}, "същото")
        assert "USE_SKILL" in v.note


class TestProgressIsNeverStopped:
    def test_same_call_with_a_changing_result_is_progress(self) -> None:
        """`RUN_CMD: pytest` пак и пак е нормално, ако изходът се мени —
        нещо е било поправено междувременно. Изходът е в ключа точно за това."""
        g = RepeatGuard()
        for i in range(STOP_AT * 4):
            v = g.observe("RUN_CMD", {"command": "pytest"}, f"{i} failed")
            assert v.stop is False
            assert v.repeats == 1

    def test_alternating_tools_never_trip_the_guard(self) -> None:
        g = RepeatGuard()
        for _ in range(STOP_AT * 3):
            assert g.observe("READ_FILE", {"path": "a"}, "A").stop is False
            assert g.observe("READ_FILE", {"path": "b"}, "B").stop is False

    def test_a_different_argument_resets_the_streak(self) -> None:
        g = RepeatGuard()
        g.observe("READ_FILE", {"path": "a"}, "same")
        g.observe("READ_FILE", {"path": "a"}, "same")
        v = g.observe("READ_FILE", {"path": "b"}, "same")
        assert v.repeats == 1
        assert v.stop is False

    def test_streak_restarts_after_an_interruption(self) -> None:
        """Две еднакви, нещо друго, после пак две еднакви — не е три подред."""
        g = RepeatGuard()
        g.observe("USE_SKILL", {"q": "x"}, "r")
        g.observe("USE_SKILL", {"q": "x"}, "r")
        g.observe("LIST_DIR", {"path": "."}, "друго")
        assert g.observe("USE_SKILL", {"q": "x"}, "r").stop is False
        assert g.observe("USE_SKILL", {"q": "x"}, "r").stop is False


class TestFingerprint:
    def test_argument_key_order_does_not_create_a_false_difference(self) -> None:
        """Моделът може да сериализира същите аргументи в различен ред."""
        g = RepeatGuard()
        g.observe("EDIT_FILE", {"a": 1, "b": 2}, "r")
        v = g.observe("EDIT_FILE", {"b": 2, "a": 1}, "r")
        assert v.repeats == 2

    def test_unserialisable_arguments_do_not_raise(self) -> None:
        g = RepeatGuard()
        v = g.observe("X", {"fn": object()}, "r")
        assert v.repeats == 1


class TestTextTagMode:
    def test_identical_text_results_stop_the_turn(self) -> None:
        g = RepeatGuard()
        out = "[RUN_CMD: ls]\nсъщият изход"
        assert g.observe_text_result(out).stop is False
        assert g.observe_text_result(out).stop is False
        assert g.observe_text_result(out).stop is True

    def test_text_mode_recovers_the_tool_name_for_the_note(self) -> None:
        g = RepeatGuard()
        for _ in range(STOP_AT):
            v = g.observe_text_result("[USE_SKILL: foo]\nняма такова")
        assert "USE_SKILL" in v.note

    def test_text_without_a_tag_header_still_works(self) -> None:
        g = RepeatGuard()
        for _ in range(STOP_AT):
            v = g.observe_text_result("гол текст без таг")
        assert v.stop is True

    def test_changing_text_results_are_progress(self) -> None:
        g = RepeatGuard()
        for i in range(STOP_AT * 3):
            assert g.observe_text_result(f"[RUN_CMD: ls]\nизход {i}").stop is False
