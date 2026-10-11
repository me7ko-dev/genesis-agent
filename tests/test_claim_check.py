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


class TestABrowserCheckNeedsABrowser:
    """2026-09-28, задача за уеб сайт: моделът прочете index.html и написа, че
    страницата се отваря без грешки в браузър. Браузър не беше пускан."""

    LIVE = ("Проверка: прегледах кода ръчно. Файловете се отварят без грешки в "
            "браузър; анимациите, темата и формата работят както е описано.")

    def test_the_live_claim_is_caught(self) -> None:
        found = unsupported_claims(self.LIVE, [("WRITE_FILE", "index.html"),
                                               ("READ_FILE", "index.html"),
                                               ("SEARCH_CODE", "</article")])
        assert [c.kind for c in found] == ["проверка в браузър"]

    def test_english_too(self) -> None:
        found = unsupported_claims("I have tested it in the browser.", [("READ_FILE", "a.html")])
        assert [c.kind for c in found] == ["проверка в браузър"]

    def test_a_headless_browser_run_proves_it(self) -> None:
        assert unsupported_claims(self.LIVE, [("WRITE_FILE", "index.html"),
                                              ("RUN_CMD", "python check_page.py  # playwright")]) == []

    def test_the_browse_tool_proves_it(self) -> None:
        assert unsupported_claims("Проверих страницата в браузъра.",
                                  [("BROWSE", "http://127.0.0.1:8000")]) == []

    def test_advice_to_open_it_is_not_a_claim(self) -> None:
        assert unsupported_claims("Отвори index.html в браузъра, за да го видиш.",
                                  [("WRITE_FILE", "index.html")]) == []


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


class TestAPromiseAtTheEndIsNotWork:
    """2026-09-28, кодинг веригата: „…и ще продължа със създаването на сайта.“ —
    ходът свърши с нула файла."""

    def test_the_live_case(self) -> None:
        text = ("Ще използвам познатите координати за село Широка лъка (приблизително "
                "41.6767° N, 24.7333° E) и ще продължа с създаването на сайта.")
        assert "ще продължа" in claim_check.unfinished_promise(text)
        assert "СЕГА" in claim_check.promise_nudge(claim_check.unfinished_promise(text))

    def test_english_too(self) -> None:
        assert claim_check.unfinished_promise("Let me now create the files.")

    def test_an_offer_to_the_operator_is_not_a_promise(self) -> None:
        assert claim_check.unfinished_promise(
            "Готово! Създадох сайта. Ако искаш, ще създам и страница за резервации.") == ""
        assert claim_check.unfinished_promise("Искате ли да продължа с галерията?") == ""

    def test_a_finished_answer_is_left_alone(self) -> None:
        assert claim_check.unfinished_promise("Създадох index.html и styles.css. Сайтът е готов.") == ""


# ── Одит 2026-10-11 (вълна 4): rc-то решава, а не текстът на изхода ─────────

def _run(cmd: str, rc: object, out: str = "") -> tuple[str, str] | None:
    """Записът, както го прави цикълът от резултата на genesis_skills._tool_run_cmd."""
    return claim_check.counts_as_executed("RUN_CMD", cmd, f"[RUN_CMD: {cmd}]  (rc={rc})\n{out}")


def _executed(*entries: tuple[str, str] | None) -> list[tuple[str, str]]:
    return [e for e in entries if e]


def _kinds(text: str, executed: list[tuple[str, str]]) -> list[str]:
    return [c.kind for c in unsupported_claims(text, executed)]


_GREEN_WITH_TRACEBACK = ("tests/test_log.py::test_logs_error PASSED\n"
                         "Traceback (most recent call last):\n  File \"app.py\"\nValueError\n"
                         "5 passed in 0.12s")


class TestTheRcHeaderDecides:
    """A1/A4: rc в заглавието казва, че командата е тръгнала и как е свършила."""

    def test_a_green_run_that_logs_a_traceback_still_counts(self) -> None:
        executed = _executed(_run("pytest -q", 0, _GREEN_WITH_TRACEBACK))
        assert executed == [("RUN_CMD", "pytest -q")]
        assert unsupported_claims("Пуснах тестовете. Всички тестове минаха.", executed) == []

    def test_a_green_run_after_a_red_one_is_the_last_word(self) -> None:
        executed = _executed(_run("pytest -q", 1, "1 failed, 4 passed"),
                             _run("pytest -q", 0, _GREEN_WITH_TRACEBACK))
        claims = unsupported_claims("Всички тестове минаха.", executed)
        assert claims == []
        assert claim_check.green_claim_after_red(claims, executed) == 0

    def test_a_green_unittest_that_prints_a_missing_file_still_counts(self) -> None:
        executed = _executed(_run("python -m unittest", 0, "файлът не съществува\nOK"))
        assert executed == [("RUN_CMD", "python -m unittest")]

    def test_a_multiline_command_reads_the_real_rc(self) -> None:
        cmd = "cd app\npytest -q"
        executed = _executed(_run("pytest -q", 0, "5 passed"), _run(cmd, 1, "1 failed, 4 passed"))
        assert _kinds("Всички тестове минаха.", executed) == ["минаващи тестове"]

    def test_a_timed_out_test_run_is_not_green(self) -> None:
        executed = _executed(_run("pytest -q", 1, "1 failed"),
                             _run("pytest -q", None, "Timeout след 120s"))
        claims = unsupported_claims("Всички тестове минаха.", executed)
        assert [c.kind for c in claims] == ["минаващи тестове"]
        assert claim_check.green_claim_after_red(claims, executed) == -1

    def test_a_timed_out_install_proves_nothing(self) -> None:
        executed = _executed(_run("pip install requests", None, "Timeout след 120s"))
        assert _kinds("Инсталирах пакета requests.", executed) == ["инсталация"]

    def test_a_blocked_command_without_a_header_is_still_not_executed(self) -> None:
        assert claim_check.counts_as_executed(
            "RUN_CMD", "pytest", "[RUN_CMD: pytest]\n[SANDBOX BLOCKED] не") is None

    def test_the_command_comes_from_the_header_not_from_args(self) -> None:
        # scripts/capability_report.py подава като args целия резултат.
        result = "[RUN_CMD: pytest -q]  (rc=1)\n1 failed"
        assert claim_check.counts_as_executed("RUN_CMD", result[:200], result) \
            == ("RUN_CMD", "pytest -q (rc=1)")


class TestOnlyATestRunProvesTests:
    """A2: паднала команда, която не пуска тестове, не доказва „пуснах тестовете“."""

    def test_failed_builds_and_installs_do_not_prove_a_test_run(self) -> None:
        for cmd in ("npm run build", "make build", "python setup.py build", "cargo build",
                    "go build ./...", "npm install", "python app.py"):
            executed = _executed(_run(cmd, 1, "error"))
            assert _kinds("Пуснах тестовете, един пада.", executed) == ["пускане на тестове"], cmd

    def test_a_failing_test_run_still_proves_the_run(self) -> None:
        executed = _executed(_run("pytest -q", 1, "1 failed, 4 passed"))
        assert unsupported_claims("Пуснах тестовете, един пада.", executed) == []


class TestTheRcOfAPipeIsNotTheRunners:
    """A3: rc на `| tail`, `|| true`, `; echo` е на опашката, не на pytest."""

    def test_a_red_run_behind_a_pipe_is_red(self) -> None:
        for cmd in ("python -m pytest -q | tail -5", "pytest -q | tee out.log",
                    "pytest -q || true", "pytest -q; echo done", "pytest -q & echo done"):
            executed = _executed(_run(cmd, 0, "1 failed, 4 passed"))
            claims = unsupported_claims("Всички тестове минаха.", executed)
            assert [c.kind for c in claims] == ["минаващи тестове"], cmd
            assert claim_check.green_claim_after_red(claims, executed) == 1, cmd

    def test_a_piped_red_run_does_not_cover_an_earlier_red_one(self) -> None:
        executed = _executed(_run("pytest -q", 1, "1 failed"),
                             _run("pytest -q | tail -3", 0, "1 failed, 4 passed"))
        assert _kinds("Всички тестове минаха.", executed) == ["минаващи тестове"]

    def test_other_runners_piped_red_are_red(self) -> None:
        for cmd, out in (("python -m unittest 2>&1 | tail", "FAILED (failures=1)"),
                         ("go test ./... | tail", "--- FAIL: TestX\nFAIL\tpkg 0.1s"),
                         ("npx jest | tail", "Tests:       1 failed, 3 passed"),
                         ("cargo test | tail", "test result: FAILED. 3 passed; 1 failed")):
            assert _run(cmd, 0, out) == ("RUN_CMD", f"{cmd} (rc=1)"), cmd

    def test_a_piped_green_run_is_green(self) -> None:
        assert _run("pytest -q 2>&1 | tail -3", 0, "5 passed") == ("RUN_CMD", "pytest -q 2>&1 | tail -3")

    def test_a_failing_commit_after_green_tests_is_not_a_red_test_run(self) -> None:
        executed = _executed(_run("pytest -q && git commit -m x", 1, "5 passed\nnothing to commit"))
        assert unsupported_claims("Всички тестове минаха.", executed) == []

    def test_a_pure_and_chain_keeps_the_runners_rc(self) -> None:
        assert _run("cd app && pytest -q 2>&1", 1, "") == ("RUN_CMD", "cd app && pytest -q 2>&1 (rc=1)")
        # Дума „FAILED“ в лога на зелено пускане с доверен rc не е червено.
        out = "WARNING FAILED to connect, retrying\n3 passed, 1 xfailed\nXFAIL test_x"
        assert _run("cd app && pytest -q", 0, out) == ("RUN_CMD", "cd app && pytest -q")


class TestTextPathKeepsTheWholeCommand:
    """A5: `[^\\]]*` режеше `[ -d tests ] && pytest -q` до `[ -d tests `."""

    def test_a_bracket_in_the_command_does_not_hide_the_test_run(self) -> None:
        executed = claim_check.executed_from_text_results([
            "[RUN_CMD: pytest -q]  (rc=0)\n5 passed",
            "[RUN_CMD: [ -d tests ] && pytest -q]  (rc=1)\n1 failed",
        ])
        assert executed[-1] == ("RUN_CMD", "[ -d tests ] && pytest -q (rc=1)")
        assert _kinds("Всички тестове минаха.", executed) == ["минаващи тестове"]


class TestRunsThatRunNoTests:
    """A6: rc=0 на команда, която не пуска тестове, не скрива червено пускане."""

    def test_check_and_no_run_flags_are_not_test_runs(self) -> None:
        for cmd in ("cargo check", "python manage.py check", "pytest --collect-only -q",
                    "pytest --version", "python -m pytest --co"):
            assert not claim_check.is_test_run(cmd), cmd
            executed = _executed(_run("cargo test", 1, "1 failed"), _run(cmd, 0, "ok"))
            assert _kinds("Всички тестове минаха.", executed) == ["минаващи тестове"], cmd

    def test_make_and_npm_check_are_still_test_runs(self) -> None:
        for cmd in ("make check", "npm run check", "python manage.py test"):
            assert claim_check.is_test_run(cmd), cmd
