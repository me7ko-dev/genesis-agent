"""genesis_agent.scheduler — zero coverage before this file, despite a real,
live round-trip bug: save_jobs_to_file() never persisted the "command" field,
so _load_jobs_from_file() always found `command=None` on the next process
start and silently skipped every single job — the entire point of writing
cron_jobs.json to disk. Fixed in the same session; the tests below pin the
fix down (`test_save_then_load_round_trip_restores_shell_jobs` is the one
that would have failed against the old code).
"""
from __future__ import annotations

import json

import pytest

from genesis_agent import scheduler as sch


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch, tmp_path):
    """Every test gets an empty job registry and its own cron_jobs.json —
    _JOBS is process-global module state, shared across tests otherwise."""
    monkeypatch.setattr(sch, "_JOBS", {})
    monkeypatch.setattr(sch, "JOBS_FILE", tmp_path / "cron_jobs.json")
    yield


# ── _parse_interval ──────────────────────────────────────────────────────

class TestParseInterval:
    def test_hourly(self) -> None:
        assert sch._parse_interval("@hourly") == 3600

    def test_daily(self) -> None:
        assert sch._parse_interval("@daily") == 86400

    def test_midnight_is_an_alias_for_daily(self) -> None:
        assert sch._parse_interval("@midnight") == 86400

    def test_weekly(self) -> None:
        assert sch._parse_interval("@weekly") == 604800

    def test_reboot_is_zero(self) -> None:
        assert sch._parse_interval("@reboot") == 0

    def test_every_n_minutes(self) -> None:
        assert sch._parse_interval("every 30m") == 1800

    def test_every_n_hours(self) -> None:
        assert sch._parse_interval("every 2h") == 7200

    def test_every_n_seconds(self) -> None:
        assert sch._parse_interval("every 45s") == 45

    def test_case_and_whitespace_insensitive(self) -> None:
        assert sch._parse_interval("  EVERY 5M  ") == 300

    def test_garbage_schedule_returns_none(self) -> None:
        assert sch._parse_interval("whenever I feel like it") is None


# ── Job / add_job / remove_job / list_jobs ──────────────────────────────

class TestJobRegistry:
    def test_add_job_registers_and_returns_it(self) -> None:
        job = sch.add_job("ping", "every 10s", lambda: None)
        assert sch._JOBS["ping"] is job
        assert job.next_run is not None

    def test_invalid_schedule_leaves_next_run_none_not_a_crash(self) -> None:
        job = sch.add_job("bad", "nonsense", lambda: None)
        assert job.next_run is None
        assert job.is_due() is False

    def test_remove_job_returns_true_when_present(self) -> None:
        sch.add_job("ping", "every 10s", lambda: None)
        assert sch.remove_job("ping") is True
        assert "ping" not in sch._JOBS

    def test_remove_job_returns_false_when_absent(self) -> None:
        assert sch.remove_job("nope") is False

    def test_list_jobs_includes_command_field(self) -> None:
        sch.add_job("func-job", "every 10s", lambda: None)
        sch.add_job("shell-job", "every 10s", lambda: None, command="echo hi")
        rows = {r["name"]: r for r in sch.list_jobs()}
        assert rows["func-job"]["command"] is None
        assert rows["shell-job"]["command"] == "echo hi"


class TestJobExecute:
    def test_execute_calls_func_and_updates_last_run(self) -> None:
        calls = []
        job = sch.add_job("x", "every 10s", lambda: calls.append(1))
        job.execute()
        assert calls == [1]
        assert job.last_run is not None

    def test_execute_swallows_exceptions_from_func(self) -> None:
        def _boom():
            raise RuntimeError("job blew up")
        job = sch.add_job("x", "every 10s", _boom)
        job.execute()  # must not raise
        assert job.last_run is not None

    def test_execute_reschedules_next_run(self) -> None:
        job = sch.add_job("x", "every 10s", lambda: None)
        first = job.next_run
        job.execute()
        assert job.next_run is not None
        assert job.next_run >= first  # rescheduled forward from "now"


# ── The persistence round-trip bug ──────────────────────────────────────

class TestSaveAndLoadRoundTrip:
    def test_save_writes_only_jobs_with_a_command(self) -> None:
        sch.add_job("func-job", "every 10s", lambda: None)          # no command
        sch.add_job("shell-job", "@hourly", lambda: None, command="echo hi")
        sch.save_jobs_to_file()

        saved = json.loads(sch.JOBS_FILE.read_text(encoding="utf-8"))
        names = [j["name"] for j in saved]
        assert names == ["shell-job"]
        assert saved[0]["command"] == "echo hi"

    def test_save_then_load_round_trip_restores_shell_jobs(self, monkeypatch) -> None:
        """The actual bug: before the fix, list_jobs() never included
        "command", so every saved job had command=None and
        _load_jobs_from_file() skipped it unconditionally on the next load."""
        sch.add_job("backup", "@daily", lambda: None, command="tar -czf backup.tar.gz .")
        sch.save_jobs_to_file()

        # Simulate a fresh process: empty registry, load from the file we just wrote.
        monkeypatch.setattr(sch, "_JOBS", {})
        sch._load_jobs_from_file()

        assert "backup" in sch._JOBS
        restored = sch._JOBS["backup"]
        assert restored.command == "tar -czf backup.tar.gz ."
        assert restored.schedule == "@daily"

    def test_loaded_shell_job_actually_runs_through_the_sandbox(self, monkeypatch) -> None:
        sch.JOBS_FILE.write_text(json.dumps([
            {"name": "greet", "schedule": "@hourly", "command": "echo hello", "enabled": True}
        ]), encoding="utf-8")

        captured = {}

        def fake_run_shell(cmd, **kw):
            captured["cmd"] = cmd
            return type("R", (), {"blocked": False, "returncode": 0, "stdout": "hello", "stderr": ""})()

        from genesis_agent import sandbox
        monkeypatch.setattr(sandbox, "run_shell", fake_run_shell)

        sch._load_jobs_from_file()
        sch._JOBS["greet"].execute()

        assert captured["cmd"] == "echo hello"

    def test_load_skips_entries_missing_a_command(self) -> None:
        sch.JOBS_FILE.write_text(json.dumps([
            {"name": "incomplete", "schedule": "@hourly"},  # no "command"
        ]), encoding="utf-8")
        sch._load_jobs_from_file()
        assert "incomplete" not in sch._JOBS

    def test_load_with_no_file_is_a_silent_no_op(self) -> None:
        sch._load_jobs_from_file()  # JOBS_FILE does not exist
        assert sch._JOBS == {}

    def test_load_with_malformed_json_does_not_raise(self) -> None:
        sch.JOBS_FILE.write_text("{not valid json", encoding="utf-8")
        sch._load_jobs_from_file()  # must not raise
        assert sch._JOBS == {}
