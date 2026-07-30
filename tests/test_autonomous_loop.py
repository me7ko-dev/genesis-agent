"""genesis_agent.autonomous_loop — the Brain -> Executor -> test-gate -> critic
mission loop, the actual autonomous product this repo builds. Every external
dependency (Brain, executor, verifier, skills_manager, dna audit, code_validate,
local_repair_agent, telemetry) is faked so these tests exercise the real
control flow with no network call, no subprocess, and no writes outside tmp_path."""
from __future__ import annotations

from typing import ClassVar

import pytest

from genesis_agent import autonomous_loop as al
from genesis_agent.brain import Brain as RealBrain
from genesis_agent.executor import ExecResult
from genesis_agent.local_repair_agent import RepairResult
from genesis_agent.skill_loader import SKILLS_ROOT
from genesis_agent.storage_monitor import StorageReport
from genesis_agent.verifier import VerifyResult


class _Reply:
    def __init__(self, raw_text: str = "", code: str | None = None, tool_calls=None) -> None:
        self.raw_text = raw_text
        self.code = code
        self.tool_calls = tool_calls
        self.usage = None


class FakeBrain:
    """Queue-driven stand-in for genesis_agent.brain.Brain: pops one canned
    reply per .complete() call (mission rounds AND critic calls share the
    same queue, in call order)."""

    trim_round_history = staticmethod(RealBrain.trim_round_history)
    replies: ClassVar[list[_Reply]] = []
    escalated = 0

    def __init__(self, *a, **kw) -> None:
        pass

    def route_for_goal(self, goal: str) -> None:
        pass

    def build_context(self, goal: str) -> str:
        return ""

    def system_prompt_base(self) -> str:
        return "system prompt"

    def escalate(self) -> bool:
        FakeBrain.escalated += 1
        return True

    def complete(self, messages, tools=None):
        assert FakeBrain.replies, "FakeBrain.complete called more times than the test queued"
        return FakeBrain.replies.pop(0)


@pytest.fixture(autouse=True)
def _fake_dependencies(monkeypatch, tmp_path):
    """Installs a fresh FakeBrain and neutralizes every side-effecting
    dependency (disk, telemetry status file) for every test in this module."""
    FakeBrain.replies = []
    FakeBrain.escalated = 0
    monkeypatch.setattr(al, "Brain", FakeBrain)
    monkeypatch.setattr(
        al,
        "check_storage",
        lambda: StorageReport(total_bytes=0, threshold_bytes=1, compression_required=False,
                               log_path=None, candidates_path=None),
    )
    monkeypatch.setattr("genesis_agent.telemetry.STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr("genesis_agent.reflection.lessons_for_prompt", lambda *a, **kw: "")
    yield


def _queue(*replies: _Reply) -> None:
    FakeBrain.replies = list(replies)


class TestEthicsShortCircuit:
    def test_harmful_goal_rejected_before_any_brain_call(self) -> None:
        # FakeBrain.replies stays empty (no _queue() call): FakeBrain.complete
        # would raise its own assertion if Brain were ever reached, so a clean
        # rejection here proves the ethics gate fired first.
        outcome = al.run_autonomous_loop("kill the background process")
        assert outcome.success is False
        assert outcome.rounds == 0
        assert "GENE-ETHICS" in outcome.last_stderr

    def test_strict_authority_rejects_unknown_operator(self, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_STRICT_AUTHORITY", "1")
        monkeypatch.delenv("GENESIS_OPERATOR", raising=False)
        outcome = al.run_autonomous_loop("write a fizzbuzz script", operator_id="rando")
        assert outcome.success is False
        assert outcome.rounds == 0
        assert "GENE-AUTHORITY" in outcome.last_stderr


class TestHappyPath:
    def test_code_runs_verifies_and_critic_approves_saves_skill(self, monkeypatch) -> None:
        code = "print('OK')"
        _queue(
            _Reply(raw_text="```python\nprint('OK')\n```", code=code),
            _Reply(raw_text="YES"),  # critic
        )
        monkeypatch.setattr(al, "run_python_subprocess",
                             lambda c: ExecResult(ok=True, stdout="OK\n", stderr="", returncode=0))
        monkeypatch.setattr("genesis_agent.code_validate.validate_code_with_ruff", lambda c: (True, ""))
        monkeypatch.setattr("genesis_agent.verifier.verify_skill",
                             lambda c: VerifyResult(verified=True, method="self_test_passed"))
        saved = {}

        def _fake_save_skill(*, slug, code, goal, verification_stdout, extra):
            saved.update(slug=slug, code=code, goal=goal, extra=extra)
            return SKILLS_ROOT / f"{slug}.md"

        monkeypatch.setattr(al, "save_skill", _fake_save_skill)

        outcome = al.run_autonomous_loop("print OK", skill_slug="my-skill")

        assert outcome.success is True
        assert outcome.rounds == 1
        assert outcome.skill_path is not None
        assert saved["slug"] == "my-skill"
        assert saved["extra"]["test_gated"] is True


class TestVerifierGate:
    def test_missing_self_test_forces_a_retry_round(self, monkeypatch) -> None:
        code_v1 = "print('no assert here')"
        code_v2 = "print('OK')\nassert True"
        _queue(
            _Reply(raw_text="```python\n" + code_v1 + "\n```", code=code_v1),
            _Reply(raw_text="```python\n" + code_v2 + "\n```", code=code_v2),
            _Reply(raw_text="YES"),  # critic on round 2
        )
        monkeypatch.setattr(al, "run_python_subprocess",
                             lambda c: ExecResult(ok=True, stdout="OK\n", stderr="", returncode=0))
        monkeypatch.setattr("genesis_agent.code_validate.validate_code_with_ruff", lambda c: (True, ""))

        verdicts = iter([
            VerifyResult(verified=False, method="no_self_test"),
            VerifyResult(verified=True, method="self_test_passed"),
        ])
        monkeypatch.setattr("genesis_agent.verifier.verify_skill", lambda c: next(verdicts))
        monkeypatch.setattr(al, "save_skill",
                             lambda **kw: SKILLS_ROOT / "x.md")

        outcome = al.run_autonomous_loop("print OK with a real self-test")

        assert outcome.success is True
        assert outcome.rounds == 2


class TestCriticGate:
    def test_critic_rejection_forces_a_retry_round(self, monkeypatch) -> None:
        code = "print('OK')"
        _queue(
            _Reply(raw_text="```python\n" + code + "\n```", code=code),
            _Reply(raw_text="NO: doesn't actually write to a file"),  # critic round 1
            _Reply(raw_text="```python\n" + code + "\n```", code=code),
            _Reply(raw_text="YES"),  # critic round 2
        )
        monkeypatch.setattr(al, "run_python_subprocess",
                             lambda c: ExecResult(ok=True, stdout="OK\n", stderr="", returncode=0))
        monkeypatch.setattr("genesis_agent.code_validate.validate_code_with_ruff", lambda c: (True, ""))
        monkeypatch.setattr("genesis_agent.verifier.verify_skill",
                             lambda c: VerifyResult(verified=True, method="self_test_passed"))
        monkeypatch.setattr(al, "save_skill",
                             lambda **kw: SKILLS_ROOT / "x.md")

        outcome = al.run_autonomous_loop("save the result to a file")

        assert outcome.success is True
        assert outcome.rounds == 2


class TestEscalation:
    def test_brain_escalates_after_a_third_of_max_rounds_fail(self, monkeypatch) -> None:
        max_rounds = 6
        # _escalate_after = max(2, max_rounds // 3) == 2 -> escalate happens
        # right after round 2 fails, before round 3 is requested.
        code = "raise RuntimeError('boom')"
        _queue(*[_Reply(raw_text="```python\n" + code + "\n```", code=code) for _ in range(max_rounds)])
        monkeypatch.setattr(al, "run_python_subprocess",
                             lambda c: ExecResult(ok=False, stdout="", stderr="boom", returncode=1))
        monkeypatch.setattr("genesis_agent.code_validate.validate_code_with_ruff", lambda c: (True, ""))
        monkeypatch.setattr(al, "emergency_repair",
                             lambda code, stderr, stdout: RepairResult(
                                 fixed=False, code=code, rounds=0, method="none", fix_desc=""))

        outcome = al.run_autonomous_loop("a goal that always fails", max_rounds=max_rounds)

        assert outcome.success is False
        assert FakeBrain.escalated == 1


class TestExhaustion:
    def test_all_rounds_fail_and_repair_also_fails_returns_failure(self, monkeypatch) -> None:
        code = "raise RuntimeError('boom')"
        max_rounds = 2
        _queue(*[_Reply(raw_text="```python\n" + code + "\n```", code=code) for _ in range(max_rounds)])
        monkeypatch.setattr(al, "run_python_subprocess",
                             lambda c: ExecResult(ok=False, stdout="", stderr="boom", returncode=1))
        monkeypatch.setattr("genesis_agent.code_validate.validate_code_with_ruff", lambda c: (True, ""))
        monkeypatch.setattr(al, "emergency_repair",
                             lambda code, stderr, stdout: RepairResult(
                                 fixed=False, code=code, rounds=0, method="none", fix_desc=""))

        outcome = al.run_autonomous_loop("a goal that always fails", max_rounds=max_rounds)

        assert outcome.success is False
        assert outcome.rounds == max_rounds
        assert outcome.skill_path is None

    def test_local_repair_saves_a_repaired_skill_after_exhaustion(self, monkeypatch) -> None:
        code = "raise RuntimeError('boom')"
        max_rounds = 2
        _queue(*[_Reply(raw_text="```python\n" + code + "\n```", code=code) for _ in range(max_rounds)])
        monkeypatch.setattr(al, "run_python_subprocess",
                             lambda c: ExecResult(ok=False, stdout="", stderr="boom", returncode=1))
        monkeypatch.setattr("genesis_agent.code_validate.validate_code_with_ruff", lambda c: (True, ""))
        monkeypatch.setattr(al, "emergency_repair",
                             lambda code, stderr, stdout: RepairResult(
                                 fixed=True, code="print('fixed')", rounds=2,
                                 method="pattern", fix_desc="added a try/except"))
        saved = {}

        def _fake_save_skill(*, slug, code, goal, verification_stdout, extra):
            saved.update(slug=slug)
            return SKILLS_ROOT / f"{slug}.md"

        monkeypatch.setattr(al, "save_skill", _fake_save_skill)

        outcome = al.run_autonomous_loop("a goal that always fails", max_rounds=max_rounds,
                                          skill_slug="broken-thing")

        assert outcome.success is True
        assert outcome.rounds == max_rounds + 2
        assert saved["slug"] == "broken-thing_repaired"


class TestPublicWrapperNeverCrashesOnNotification:
    def test_reflection_and_notifier_failures_do_not_propagate(self, monkeypatch) -> None:
        code = "print('OK')"
        _queue(
            _Reply(raw_text="```python\n" + code + "\n```", code=code),
            _Reply(raw_text="YES"),
        )
        monkeypatch.setattr(al, "run_python_subprocess",
                             lambda c: ExecResult(ok=True, stdout="OK\n", stderr="", returncode=0))
        monkeypatch.setattr("genesis_agent.code_validate.validate_code_with_ruff", lambda c: (True, ""))
        monkeypatch.setattr("genesis_agent.verifier.verify_skill",
                             lambda c: VerifyResult(verified=True, method="self_test_passed"))
        monkeypatch.setattr(al, "save_skill",
                             lambda **kw: SKILLS_ROOT / "x.md")

        def _boom(*a, **kw):
            raise RuntimeError("Discord webhook unreachable")

        monkeypatch.setattr("genesis_agent.reflection.record_mission", _boom)
        monkeypatch.setattr("genesis_agent.notifier.notify", _boom)

        outcome = al.run_autonomous_loop("print OK")

        assert outcome.success is True
