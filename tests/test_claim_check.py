"""genesis_agent.claim_check — catches the agent SIMULATING work: claiming an
action is done when nothing that could have done it ever ran.

This is the most expensive way an agent fails. "готово, преместих снимките"
with no move in the transcript is believed, and the operator stops checking.
The repo already carries two fixes for this bug class ("Fix Genesis handing
work back instead of doing it", "Fix local model narrating tool use without
ever executing anything") and a system_prompt rule against it — but the only
code-level check was `rounds == 0`, which a single harmless LIST_DIR defeats.

Two failure directions matter equally here:
  - a miss lets a simulated result through (what this module exists for);
  - a false alarm interrupts real work and teaches the operator to ignore
    the warning, which is worse than not having it.
So the false-alarm cases below are not padding — they are the constraint.
"""
from __future__ import annotations

from genesis_agent import claim_check
from genesis_agent.claim_check import Claim, nudge_text, unsupported_claims


class TestCatchesSimulatedWork:
    def test_the_case_the_old_round_counter_missed(self) -> None:
        """One harmless LIST_DIR made rounds=1, and from there any claim
        passed. That is the exact hole this module was written to close."""
        found = unsupported_claims(
            "Готово — инсталирах пакета и преместих файловете.",
            [("LIST_DIR", "/home/user")],
        )
        assert {c.kind for c in found} == {"инсталация", "преместване/триене"}

    def test_a_run_cmd_of_the_wrong_kind_does_not_prove_an_install(self) -> None:
        found = unsupported_claims("Инсталирах ruff.", [("RUN_CMD", "ls -la")])
        assert [c.kind for c in found] == ["инсталация"]

    def test_nothing_executed_at_all(self) -> None:
        found = unsupported_claims("Създадох файла и пуснах тестовете.", [])
        assert {c.kind for c in found} == {"запис на файл", "пускане на тестове"}

    def test_english_claims_are_caught_too(self) -> None:
        found = unsupported_claims("I've installed the dependencies.", [("READ_FILE", "a.py")])
        assert [c.kind for c in found] == ["инсталация"]

    def test_the_quote_points_at_the_actual_sentence(self) -> None:
        found = unsupported_claims("Първо погледнах. После инсталирах ruff. Край.", [])
        assert "инсталирах ruff" in found[0].quote


class TestDoesNotCryWolf:
    """A false alarm interrupts real work — these pin down what must pass."""

    def test_a_real_install_is_accepted(self) -> None:
        assert unsupported_claims("Инсталирах ruff.", [("RUN_CMD", "pip install ruff")]) == []

    def test_write_file_proves_a_write(self) -> None:
        assert unsupported_claims("Създадох config.py", [("WRITE_FILE", "config.py")]) == []

    def test_edit_file_proves_a_write(self) -> None:
        assert unsupported_claims("Записах промяната.", [("EDIT_FILE", "main.py")]) == []

    def test_a_real_test_run_is_accepted(self) -> None:
        assert unsupported_claims("Пуснах тестовете, всичко минава.",
                                  [("RUN_CMD", "python -m pytest -q")]) == []

    def test_a_real_move_is_accepted(self) -> None:
        assert unsupported_claims("Преместих файловете.",
                                  [("RUN_CMD", "mv /a/*.jpg /b/")]) == []

    def test_plain_description_without_a_past_tense_claim_is_ignored(self) -> None:
        """Talking about what COULD be done is not claiming it was done."""
        assert unsupported_claims("Мога да инсталирам ruff, ако искаш.", []) == []
        assert unsupported_claims("Ще преместя файловете след потвърждение.", []) == []

    def test_delegated_and_skill_work_counts_as_execution(self) -> None:
        assert unsupported_claims("Инсталирах го.", [("DELEGATE", "set up the env")]) == []
        assert unsupported_claims("Създадох отчета.", [("USE_SKILL", "report_builder")]) == []

    def test_empty_text_is_not_a_claim(self) -> None:
        assert unsupported_claims("", [("RUN_CMD", "ls")]) == []


class TestNudgeText:
    def test_it_names_the_specific_claim_not_just_a_generic_scolding(self) -> None:
        """A vague "you didn't prove it" makes a weak model rephrase instead
        of actually running the command."""
        text = nudge_text([Claim(kind="инсталация", quote="инсталирах ruff",
                                 needed="RUN_CMD")])
        assert "инсталация" in text
        assert "инсталирах ruff" in text
        assert "RUN_CMD" in text

    def test_it_allows_for_work_done_in_an_earlier_round(self) -> None:
        """Without this escape hatch the check would fight legitimate
        summaries of work executed several rounds back."""
        text = nudge_text([Claim(kind="инсталация", quote="q", needed="RUN_CMD")])
        assert "по-ранен рунд" in text


class TestAFailedToolIsNotEvidence:
    """Най-острият пропуск в първата версия: записваше се ОПИТЪТ, не
    резултатът. Моделът иска инсталация, sandbox я блокира, моделът обявява
    "инсталирах пакета" — и проверката мълчеше, защото извикване е имало.
    Точно сценарият, за който тя съществува."""

    def test_a_blocked_command_does_not_prove_an_install(self) -> None:
        assert claim_check.counts_as_executed(
            "RUN_CMD", "sudo apt install nmap",
            "[RUN_CMD: ...] [SANDBOX BLOCKED] катастрофална команда") is None

    def test_a_declined_command_does_not_prove_anything(self) -> None:
        assert claim_check.counts_as_executed(
            "RUN_CMD", "rm -rf /tmp/x", "[SANDBOX DECLINED] операторът отказа") is None

    def test_a_failed_write_does_not_prove_a_write(self) -> None:
        assert claim_check.counts_as_executed(
            "WRITE_FILE", "/etc/hosts", "[TOOL] Грешка при изпълнение: Permission denied"
        ) is None

    def test_a_successful_command_does_count(self) -> None:
        assert claim_check.counts_as_executed(
            "RUN_CMD", "pip install ruff", "Successfully installed ruff-0.16.8"
        ) == ("RUN_CMD", "pip install ruff")

    def test_end_to_end_a_blocked_install_is_still_challenged(self) -> None:
        entry = claim_check.counts_as_executed(
            "RUN_CMD", "sudo apt install nmap", "[SANDBOX BLOCKED] отказано")
        found = claim_check.unsupported_claims(
            "Готово, инсталирах пакета.", [e for e in [entry] if e])
        assert [c.kind for c in found] == ["инсталация"]


class TestTextResultParsing:
    def test_it_extracts_the_tool_name_from_the_result_prefix(self) -> None:
        got = claim_check.executed_from_text_results(["[READ_FILE: /a/b.py]\nсъдържание"])
        assert got == [("READ_FILE", "/a/b.py")]

    def test_failed_results_are_excluded(self) -> None:
        got = claim_check.executed_from_text_results([
            "[RUN_CMD: ls]\nfile1", "[RUN_CMD: rm -rf /]\n[SANDBOX BLOCKED] не"])
        assert got == [("RUN_CMD", "ls")]

    def test_unrecognised_shapes_are_skipped_not_guessed(self) -> None:
        assert claim_check.executed_from_text_results(["просто текст", "", None]) == []
