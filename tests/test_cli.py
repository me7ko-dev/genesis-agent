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


def test_discord_delegates_to_discord_bot_main(monkeypatch) -> None:
    pytest.importorskip("discord")  # optional dependency; discord_bot.py SystemExits without it
    called = []
    monkeypatch.setattr("genesis_agent.discord_bot.main", lambda: called.append(True))
    assert cli_mod.main(["discord"]) == 0
    assert called == [True]


class TestGuiVoiceMissingScript:
    def test_missing_gui_script_prints_clone_hint_and_returns_1(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr("genesis_agent.paths.PACKAGE_DIR", tmp_path)
        rc = cli_mod.main(["gui"])
        assert rc == 1
        assert "git clone" in capsys.readouterr().out


class TestFixArgParsing:
    """_fix() owns a hand-rolled argv parser — the highest-risk part of this
    file, since a misparsed flag silently changes which model tier or test
    command a real repair run uses."""

    def _patch_repair(self, monkeypatch, capture: dict):
        def fake_repair(project, task, *, test_command=None, max_rounds=8, quality=None):
            capture.update(project=project, task=task, test_command=test_command,
                           max_rounds=max_rounds, quality=quality)
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
