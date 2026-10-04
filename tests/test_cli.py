"""genesis_agent.cli — the `genesis` command's dispatcher, zero coverage
before this file despite being the single choke point every user-facing
entrypoint goes through (`genesis mission`, `genesis fix`, `genesis setup`,
...). A routing bug here breaks the command entirely, not just one feature.

Every branch here delegates to another module immediately, so these tests
monkeypatch the delegate (never a real mission/repair/setup run) and assert
on: which delegate got called, with what arguments, and that main()'s return
code matches what the delegate reported.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import genesis_agent.cli as cli_mod


def test_help_prints_usage_and_returns_0(capsys) -> None:
    for args in (["-h"], ["--help"], ["help"]):
        rc = cli_mod.main(args)
        assert rc == 0
        assert "genesis" in capsys.readouterr().out


def test_version_flag_returns_0(capsys) -> None:
    for args in (["-V"], ["--version"], ["version"]):
        rc = cli_mod.main(args)
        assert rc == 0
        assert cli_mod.__version__ in capsys.readouterr().out


class TestDebugLog:
    """Местата, които нарочно не спират работата при грешка, я записват с
    log.debug(exc_info=True). GENESIS_DEBUG=1 е начинът тя да се види."""

    @pytest.fixture
    def genesis_logger(self):
        import logging
        logger = logging.getLogger("genesis")
        before = (list(logger.handlers), logger.level)
        yield logger
        for h in logger.handlers[:]:
            if h not in before[0]:
                logger.removeHandler(h)
                h.close()
        logger.setLevel(before[1])

    def test_on_it_writes_swallowed_errors_to_a_file(self, monkeypatch, tmp_path, genesis_logger) -> None:
        import logging
        monkeypatch.setenv("GENESIS_DEBUG", "1")
        monkeypatch.setattr("genesis_agent.paths.GENESIS_HOME", tmp_path)
        assert cli_mod.main(["--version"]) == 0
        try:
            raise ValueError("нарочно")
        except ValueError:
            logging.getLogger("genesis.terminal").debug("паметта не записа", exc_info=True)
        for h in genesis_logger.handlers:
            h.flush()
        text = (tmp_path / "debug.log").read_text(encoding="utf-8")
        assert "genesis.terminal" in text and "паметта не записа" in text
        assert "ValueError: нарочно" in text

    def test_off_by_default(self, monkeypatch, tmp_path, genesis_logger) -> None:
        monkeypatch.delenv("GENESIS_DEBUG", raising=False)
        monkeypatch.setattr("genesis_agent.paths.GENESIS_HOME", tmp_path)
        cli_mod.main(["--version"])
        assert not (tmp_path / "debug.log").exists()


def test_no_args_defaults_to_chat(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(cli_mod, "_chat", lambda: called.append(True) or 0)
    assert cli_mod.main([]) == 0
    assert called == [True]


def test_unknown_command_returns_2_and_prints_usage(capsys) -> None:
    rc = cli_mod.main(["frobnicate"])
    assert rc == 2
    assert "Непозната команда" in capsys.readouterr().out


def test_setup_delegates_to_setup_wizard(monkeypatch) -> None:
    monkeypatch.setattr("genesis_agent.setup_wizard.run", lambda: 0)
    assert cli_mod.main(["setup"]) == 0

    monkeypatch.setattr("genesis_agent.setup_wizard.run", lambda: 1)
    assert cli_mod.main(["setup"]) == 1


class TestMission:
    def test_missing_goal_returns_2_without_calling_the_loop(self, monkeypatch, capsys) -> None:
        called = []
        monkeypatch.setattr("genesis_agent.autonomous_loop.run_autonomous_loop",
                            lambda goal: called.append(goal))
        rc = cli_mod.main(["mission"])
        assert rc == 2
        assert called == []

    def test_goal_is_joined_and_passed_through(self, monkeypatch) -> None:
        received = []

        def fake_loop(goal):
            received.append(goal)
            return SimpleNamespace(success=True, rounds=3, skill_path="")

        monkeypatch.setattr("genesis_agent.autonomous_loop.run_autonomous_loop", fake_loop)
        rc = cli_mod.main(["mission", "write", "a", "retry", "decorator"])
        assert rc == 0
        assert received == ["write a retry decorator"]

    def test_failed_mission_returns_1(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.autonomous_loop.run_autonomous_loop",
                            lambda goal: SimpleNamespace(success=False, rounds=8, skill_path=""))
        assert cli_mod.main(["mission", "do", "something", "impossible"]) == 1


def test_skills_prints_verified_count(monkeypatch, capsys) -> None:
    monkeypatch.setattr("genesis_agent.skill_loader.load_skills_index", lambda: {
        "a": {"verified": True},
        "b": {"verified": False},
        "c": {"verified": True},
    })
    rc = cli_mod.main(["skills"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "3" in out and "2" in out


class TestBudgetCommand:
    def test_prints_today_and_range_reports(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr("genesis_agent.budget.today_totals", lambda: {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cached_read_tokens": 0, "cached_write_tokens": 0, "by_provider": {}})
        seen_days = []
        monkeypatch.setattr("genesis_agent.budget.range_totals", lambda days: (
            seen_days.append(days) or {
                "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "cached_read_tokens": 0, "cached_write_tokens": 0, "by_provider": {}}))
        rc = cli_mod.main(["budget"])
        assert rc == 0
        assert seen_days == [7]
        out = capsys.readouterr().out
        assert "Днес" in out and "Последните 7 дни" in out

    def test_custom_day_count(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr("genesis_agent.budget.today_totals", lambda: {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cached_read_tokens": 0, "cached_write_tokens": 0, "by_provider": {}})
        seen_days = []
        monkeypatch.setattr("genesis_agent.budget.range_totals", lambda days: (
            seen_days.append(days) or {
                "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "cached_read_tokens": 0, "cached_write_tokens": 0, "by_provider": {}}))
        rc = cli_mod.main(["budget", "30"])
        assert rc == 0
        assert seen_days == [30]

    def test_non_numeric_day_count_is_rejected(self, capsys) -> None:
        rc = cli_mod.main(["budget", "abc"])
        assert rc == 2
        assert "abc" in capsys.readouterr().out


class TestRemovedFrontends:
    @pytest.mark.parametrize("cmd", ["gui", "voice"])
    def test_says_it_was_removed_instead_of_unknown_command(self, cmd, capsys) -> None:
        # „Непозната команда" на нещо, което доскоро работеше, изглежда като
        # счупена инсталация — затова изрично казва, че е махнато.
        rc = cli_mod.main([cmd])
        assert rc == 2
        out = capsys.readouterr().out
        assert "махнат" in out
        assert "Непозната" not in out


class TestFixArgParsing:
    """_fix() owns a hand-rolled argv parser — the highest-risk part of this
    file, since a misparsed flag silently changes which model tier or test
    command a real repair run uses."""

    def _patch_repair(self, monkeypatch, capture: dict):
        def fake_repair(project, task, *, test_command=None, max_rounds=8, quality=None,
                        checkpoint=False):
            capture.update(project=project, task=task, test_command=test_command,
                           max_rounds=max_rounds, quality=quality, checkpoint=checkpoint)
            return SimpleNamespace(success=True)

        monkeypatch.setattr("genesis_agent.repo_agent.repair", fake_repair)
        monkeypatch.setattr("genesis_agent.repo_agent.format_outcome",
                            lambda out, show_diff=True: "outcome")

    def test_no_args_prints_usage_returns_2(self, capsys) -> None:
        rc = cli_mod.main(["fix"])
        assert rc == 2
        assert "Употреба" in capsys.readouterr().out

    def test_help_flag_prints_usage_returns_0(self, capsys) -> None:
        rc = cli_mod.main(["fix", "-h"])
        assert rc == 0

    def test_missing_bug_description_returns_2(self, monkeypatch) -> None:
        capture: dict = {}
        self._patch_repair(monkeypatch, capture)
        rc = cli_mod.main(["fix", "/some/project"])
        assert rc == 2
        assert capture == {}  # repair() never called

    def test_defaults_when_no_flags_given(self, monkeypatch) -> None:
        capture: dict = {}
        self._patch_repair(monkeypatch, capture)
        rc = cli_mod.main(["fix", "/proj", "median()", "is", "wrong"])
        assert rc == 0
        assert capture["project"] == "/proj"
        assert capture["task"] == "median() is wrong"
        assert capture["test_command"] is None
        assert capture["max_rounds"] == 8
        assert capture["quality"] is None
        assert capture["checkpoint"] is False  # бекъп само при поискване

    def test_checkpoint_flag_asks_for_a_snapshot(self, monkeypatch) -> None:
        capture: dict = {}
        self._patch_repair(monkeypatch, capture)
        cli_mod.main(["fix", "/proj", "--checkpoint", "fix", "it"])
        assert capture["checkpoint"] is True
        assert capture["task"] == "fix it"

    def test_maxcoding_flag_sets_coding_quality(self, monkeypatch) -> None:
        capture: dict = {}
        self._patch_repair(monkeypatch, capture)
        cli_mod.main(["fix", "/proj", "--maxcoding", "fix", "the", "bug"])
        assert capture["quality"] == "coding"
        assert capture["task"] == "fix the bug"

    def test_max_flag_sets_max_quality(self, monkeypatch) -> None:
        capture: dict = {}
        self._patch_repair(monkeypatch, capture)
        cli_mod.main(["fix", "/proj", "--max", "fix", "it"])
        assert capture["quality"] == "max"

    def test_test_command_flag_consumes_its_value(self, monkeypatch) -> None:
        capture: dict = {}
        self._patch_repair(monkeypatch, capture)
        cli_mod.main(["fix", "/proj", "--test", "npm test -- --run", "fix", "it"])
        assert capture["test_command"] == "npm test -- --run"
        assert capture["task"] == "fix it"

    def test_rounds_flag_parses_int(self, monkeypatch) -> None:
        capture: dict = {}
        self._patch_repair(monkeypatch, capture)
        cli_mod.main(["fix", "/proj", "--rounds", "3", "fix", "it"])
        assert capture["max_rounds"] == 3

    def test_rounds_flag_rejects_non_numeric_value(self, monkeypatch, capsys) -> None:
        capture: dict = {}
        self._patch_repair(monkeypatch, capture)
        rc = cli_mod.main(["fix", "/proj", "--rounds", "banana", "fix", "it"])
        assert rc == 2
        assert capture == {}

    def test_failed_repair_returns_1(self, monkeypatch) -> None:
        monkeypatch.setattr("genesis_agent.repo_agent.repair",
                            lambda project, task, **kw: SimpleNamespace(success=False))
        monkeypatch.setattr("genesis_agent.repo_agent.format_outcome",
                            lambda out, show_diff=True: "outcome")
        rc = cli_mod.main(["fix", "/proj", "fix", "it"])
        assert rc == 1

    def test_revert_without_path_returns_2(self, capsys) -> None:
        rc = cli_mod.main(["fix", "--revert"])
        assert rc == 2

    def test_revert_delegates_to_restore_checkpoint(self, monkeypatch) -> None:
        called = []
        monkeypatch.setattr("genesis_agent.repo_agent.restore_checkpoint",
                            lambda path: called.append(path) or "restored")
        rc = cli_mod.main(["fix", "--revert", "/proj"])
        assert rc == 0
        assert called == ["/proj"]


def test_pack_zips_the_project_and_prints_the_report_lines(tmp_path, capsys) -> None:
    proj = tmp_path / "site"
    proj.mkdir()
    (proj / "app.py").write_text("print(1)\n", encoding="utf-8")
    (proj / ".env").write_text("KEY=x\n", encoding="utf-8")
    assert cli_mod.main(["pack", str(proj), "--no-tests"]) == 0
    out = capsys.readouterr().out
    zips = list(tmp_path.glob("site-*.zip"))
    assert len(zips) == 1 and str(zips[0]) in out
    assert "1 файл" in out and ".env" in out


def test_pack_returns_1_when_the_projects_tests_fail(tmp_path, capsys) -> None:
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_a.py").write_text("def test_a():\n    assert False\n", encoding="utf-8")
    assert cli_mod.main(["pack", str(proj), "-o", str(tmp_path / "out.zip")]) == 1
    assert (tmp_path / "out.zip").is_file() and "❌" in capsys.readouterr().out


def test_pack_of_a_missing_folder_says_so(tmp_path, capsys) -> None:
    assert cli_mod.main(["pack", str(tmp_path / "nope")]) == 2
    assert "Няма такава папка" in capsys.readouterr().out
