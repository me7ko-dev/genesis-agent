"""genesis_agent.delegate — sub-agent task delegation, the backend for the
[DELEGATE: ...] tool tag dispatched from genesis_skills.py. Zero coverage
before this file. Real background threads are used throughout (that's the
point of the module) rather than mocked away, except where a real sandboxed
subprocess would make a test slow or flaky (the timeout path).
"""
from __future__ import annotations

import time

import pytest

import genesis_agent.delegate as dm


@pytest.fixture(autouse=True)
def _clean_registry():
    """_TASKS is module-level global state shared across tests."""
    dm._TASKS.clear()
    yield
    dm._TASKS.clear()


class TestDelegateTaskShellAgent:
    def test_successful_shell_command_reports_done_with_stdout(self) -> None:
        task = dm.delegate_task("echo hello-from-delegate", agent="shell")
        dm.wait_all([task], timeout=10)
        assert task.status == dm.TaskStatus.DONE
        assert "hello-from-delegate" in task.result
        assert task.error is None

    def test_blocked_shell_command_reports_failed(self) -> None:
        task = dm.delegate_task("rm -rf /", agent="shell")
        dm.wait_all([task], timeout=10)
        assert task.status == dm.TaskStatus.FAILED
        assert task.error is not None

    def test_registered_in_get_status_and_list_tasks(self) -> None:
        task = dm.delegate_task("echo x", agent="shell")
        assert dm.get_status(task.id) is task
        assert task in dm.list_tasks()
        dm.wait_all([task], timeout=10)

    def test_unknown_agent_type_fails_with_a_clear_message(self) -> None:
        task = dm.delegate_task("whatever", agent="not-a-real-agent")
        dm.wait_all([task], timeout=10)
        assert task.status == dm.TaskStatus.FAILED
        assert "Непознат agent тип" in task.error


class TestWaitAllTimeout:
    def test_still_running_task_is_marked_timeout(self, monkeypatch) -> None:
        monkeypatch.setattr(dm, "_run_shell", lambda goal: time.sleep(0.3) or "done-late")
        task = dm.delegate_task("irrelevant", agent="shell")
        dm.wait_all([task], timeout=0.01)
        assert task.status == dm.TaskStatus.TIMEOUT
        time.sleep(0.4)  # let the background thread actually finish before the test exits

    def test_fast_task_within_timeout_is_not_marked_timeout(self) -> None:
        task = dm.delegate_task("echo quick", agent="shell")
        dm.wait_all([task], timeout=10)
        assert task.status != dm.TaskStatus.TIMEOUT


class TestDelegatedTaskElapsed:
    def test_none_before_started(self) -> None:
        task = dm.DelegatedTask(id="x", goal="g", agent="shell")
        assert task.elapsed is None

    def test_reflects_duration_after_finishing(self) -> None:
        task = dm.delegate_task("echo x", agent="shell")
        dm.wait_all([task], timeout=10)
        assert task.elapsed is not None
        assert task.elapsed >= 0


class TestSummary:
    def test_includes_icon_agent_and_goal(self) -> None:
        task = dm.DelegatedTask(id="abcdef1234567890", goal="a shell thing", agent="shell",
                                status=dm.TaskStatus.DONE)
        s = task.summary()
        assert "✅" in s
        assert "shell" in s
        assert "a shell thing" in s
        assert "abcdef12" in s  # id truncated to first 8 chars


class TestRunParallel:
    def test_runs_multiple_goals_and_waits_for_all(self) -> None:
        results = dm.run_parallel(["echo one", "echo two", "echo three"], agent="shell", timeout=10)
        assert len(results) == 3
        assert all(t.status == dm.TaskStatus.DONE for t in results)
        outputs = {t.result.strip() for t in results}
        assert outputs == {"one", "two", "three"}
