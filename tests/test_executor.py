"""genesis_agent.executor — runs LLM-produced Python and captures its
output. Zero coverage before this file despite being one of the two modules
the codebase's own ruff-ignore comment names as reliability-critical
(alongside sandbox/brain/skills_api/budget): every autonomous
mission round and every `genesis fix` repair round goes through this.

What matters here: the DNA gate actually blocks before anything runs (both
execution paths), success/failure are reported correctly with the real
stdout/stderr/returncode, and format_failure_for_brain's truncation keeps the
tail of the output (where the real exception lives), not the head.
"""
from __future__ import annotations

import pytest

from genesis_agent import dna, executor


@pytest.fixture(autouse=True)
def _no_red_zone_elevation(monkeypatch):
    """Both unset would otherwise compare None == None and silently
    elevate (dna.red_zone_elevation_granted's own docstring warning) — pin
    both explicitly so the gate tests below are deterministic regardless of
    what the real dev machine happens to have exported."""
    monkeypatch.delenv("GENESIS_RED_ZONE_TOKEN", raising=False)
    monkeypatch.delenv("GENESIS_RED_ZONE_SECRET", raising=False)


@pytest.fixture
def _isolated_sandbox(tmp_path, monkeypatch):
    sandbox_dir = tmp_path / "sandbox"
    monkeypatch.setattr(executor, "SANDBOX_DIR", sandbox_dir)
    return sandbox_dir


class TestRunPythonSubprocess:
    def test_successful_code_reports_ok_with_real_stdout(self, _isolated_sandbox) -> None:
        result = executor.run_python_subprocess("print('hello from subprocess')")
        assert result.ok is True
        assert "hello from subprocess" in result.stdout
        assert result.returncode == 0

    def test_raising_code_reports_failure_with_traceback_in_stderr(self, _isolated_sandbox) -> None:
        result = executor.run_python_subprocess("raise ValueError('boom')")
        assert result.ok is False
        assert "ValueError" in result.stderr
        assert "boom" in result.stderr

    def test_ensures_the_sandbox_directory_exists(self, _isolated_sandbox) -> None:
        assert not _isolated_sandbox.exists()
        executor.run_python_subprocess("print(1)")
        assert _isolated_sandbox.is_dir()

    def test_dna_gate_blocks_before_the_sandbox_ever_runs(self, _isolated_sandbox, monkeypatch) -> None:
        """The gate must short-circuit — proven here by making the sandbox
        call itself fail the test if it's ever reached."""
        def _must_not_run(*args, **kwargs):
            raise AssertionError("sandbox.run_python must not be called when the DNA gate refuses")
        monkeypatch.setattr("genesis_agent.sandbox.run_python", _must_not_run)

        result = executor.run_python_subprocess("x = 'HKEY_LOCAL_MACHINE'")

        assert result.ok is False
        assert "[GENESIS DNA]" in result.stderr
        assert result.returncode is None


class TestRunPythonInprocess:
    def test_successful_code_reports_ok_with_captured_stdout(self) -> None:
        result = executor.run_python_inprocess("print('hello from in-process')")
        assert result.ok is True
        assert result.stdout.strip() == "hello from in-process"
        assert result.returncode == 0

    def test_raising_code_reports_failure_with_traceback_in_stderr(self) -> None:
        result = executor.run_python_inprocess("raise ValueError('boom')")
        assert result.ok is False
        assert result.returncode == 1
        assert "ValueError" in result.stderr
        assert "boom" in result.stderr

    def test_dna_gate_blocks_before_anything_executes(self) -> None:
        # If the gate failed to short-circuit, `sentinel` (undefined) would
        # raise NameError inside exec() and get caught as a normal runtime
        # failure — ok=False either way, so the real proof is that stdout
        # stays empty and the message is the DNA one, not a traceback.
        result = executor.run_python_inprocess("x = 'HKEY_LOCAL_MACHINE'\nsentinel")
        assert result.ok is False
        assert "[GENESIS DNA]" in result.stderr
        assert "NameError" not in result.stderr
        assert result.stdout == ""

class TestValidateCodeBeforeExecution:
    """Both of executor.py's callers convert a truthy return into a graceful
    ExecResult — this function must return a message string, never raise,
    or the mission/repair loop calling into executor sees an uncaught
    exception instead (bug found 2026-09-18, see dna.py's docstring)."""

    def test_clean_code_returns_none(self) -> None:
        assert dna.validate_code_before_execution("print(1)") is None

    def test_red_zone_code_without_elevation_returns_a_message_not_raises(self) -> None:
        reason = dna.validate_code_before_execution("x = 'HKEY_LOCAL_MACHINE'")
        assert isinstance(reason, str) and reason

    def test_red_zone_code_with_elevation_granted_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_RED_ZONE_TOKEN", "shared-secret")
        monkeypatch.setenv("GENESIS_RED_ZONE_SECRET", "shared-secret")
        assert dna.validate_code_before_execution("x = 'HKEY_LOCAL_MACHINE'") is None


class TestFormatFailureForBrain:
    def test_no_output_at_all(self) -> None:
        result = executor.ExecResult(ok=True, stdout="", stderr="", returncode=None)
        assert executor.format_failure_for_brain(result) == "(no output captured)"

    def test_includes_stderr_stdout_and_returncode_sections(self) -> None:
        result = executor.ExecResult(ok=False, stdout="printed stuff", stderr="the error",
                                      returncode=1)
        out = executor.format_failure_for_brain(result)
        assert "### stderr\nthe error" in out
        assert "### stdout\nprinted stuff" in out
        assert "### return code\n1" in out

    def test_returncode_none_omits_that_section(self) -> None:
        result = executor.ExecResult(ok=False, stdout="", stderr="boom", returncode=None)
        out = executor.format_failure_for_brain(result)
        assert "### return code" not in out

    def test_long_output_is_truncated_keeping_the_tail_not_the_head(self) -> None:
        # The real exception is always at the end of a traceback — truncating
        # from the front would cut the one line that actually matters.
        stderr = "".join(f"line {i}\n" for i in range(500)) + "ValueError: the actual error"
        result = executor.ExecResult(ok=False, stdout="", stderr=stderr, returncode=1)
        out = executor.format_failure_for_brain(result)
        assert "...(truncated)..." in out
        assert "ValueError: the actual error" in out
        assert "line 0\n" not in out
