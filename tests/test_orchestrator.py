"""genesis_agent.orchestrator — the multi-agent (Planner/Coder/Tester/Reviewer)
loop behind `parallel_forge.py` (the skill-forge documented in README.md) and
`benchmark.py`. Zero coverage before this file despite being on that real path.

Brain itself is never exercised here — every test replaces
`orchestrator.Brain` with a scripted fake that returns canned replies in
order, so what's actually under test is the orchestration: does a DNA-ethics
rejection short-circuit before any LLM call happens, does a failed round
correctly trigger the Reviewer and retry, does a native tool_call get
dispatched instead of treated as a missing code block, does the loop give up
cleanly at max_rounds.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import genesis_agent.orchestrator as orch_mod
from genesis_agent.brain import Brain as _RealBrain
from genesis_agent.orchestrator import (
    OrchestratedOutcome,
    _extract_code,
    run_orchestrated,
)


def _reply(raw_text: str = "", code: str = "", tool_calls=None) -> SimpleNamespace:
    return SimpleNamespace(raw_text=raw_text, code=code, tool_calls=tool_calls)


class _FakeBrain:
    """Stand-in for genesis_agent.brain.Brain. `replies` is popped in call
    order by .complete() — one entry per Planner/Coder/Reviewer turn.

    trim_round_history is called as `Brain.trim_round_history(...)` — a
    class-level call, not on an instance — so orchestrator.Brain must be a
    real class with that attribute, not a plain factory function. Reusing
    the real (pure, already-tested-elsewhere) implementation here rather
    than reimplementing it.
    """

    trim_round_history = staticmethod(_RealBrain.trim_round_history)

    def __init__(self, replies: list[SimpleNamespace], **kw) -> None:
        self._replies = list(replies)
        self.init_kwargs = kw
        self.complete_calls: list[list[dict]] = []

    def route_for_goal(self, goal: str) -> None:
        pass

    def build_context(self, goal: str) -> str:
        return ""

    def system_prompt_base(self) -> str:
        return "sys"

    def complete(self, messages, tools=None):
        self.complete_calls.append(messages)
        if not self._replies:
            raise AssertionError("complete() called more times than replies were scripted")
        return self._replies.pop(0)


@pytest.fixture(autouse=True)
def _no_real_ethics_or_reflection(monkeypatch):
    """Ethics/operator gate defaults to a no-op pass; reflection lessons are
    optional and irrelevant to these tests either way."""
    monkeypatch.setattr(orch_mod.dna, "validate_goal_ethics", lambda goal: None)
    monkeypatch.setattr(orch_mod.dna, "assert_operator_if_strict", lambda operator_id: None)


def _install_fake_brain(monkeypatch, replies: list[SimpleNamespace]) -> list[_FakeBrain]:
    instances: list[_FakeBrain] = []

    class _Bound(_FakeBrain):
        def __init__(self, **kw) -> None:
            super().__init__(replies, **kw)
            instances.append(self)

    monkeypatch.setattr(orch_mod, "Brain", _Bound)
    return instances


class TestExtractCode:
    def test_extracts_fenced_python_block(self) -> None:
        raw = "here you go:\n```python\ndef f():\n    return 1\n```\nthat's it"
        assert _extract_code(raw) == "def f():\n    return 1"

    def test_bare_def_without_fence_is_used_as_is(self) -> None:
        raw = "def f():\n    return 1"
        assert _extract_code(raw) == raw

    def test_prose_with_no_code_returns_empty(self) -> None:
        assert _extract_code("I think the answer is 42.") == ""


class TestEthicsGate:
    def test_rejected_goal_short_circuits_before_any_llm_call(self, monkeypatch) -> None:
        def _reject(goal):
            raise orch_mod.dna.GenesisDNAError("goal violates policy")

        monkeypatch.setattr(orch_mod.dna, "validate_goal_ethics", _reject)
        instances = _install_fake_brain(monkeypatch, [])  # no replies scripted — must not be used
        out = run_orchestrated("do something forbidden")
        assert out.success is False
        assert out.rounds == 0
        assert "goal violates policy" in out.last_error
        assert instances[0].complete_calls == []


class TestHappyPath:
    def test_first_round_success_reports_one_round_and_skill_path(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(orch_mod, "SKILLS_ROOT", tmp_path)
        skill_file = tmp_path / "reverse_a_string.md"

        monkeypatch.setattr(orch_mod, "run_python_subprocess",
                            lambda code: SimpleNamespace(ok=True, stdout="OK", stderr="", returncode=0))
        monkeypatch.setattr(orch_mod, "verify_skill",
                            lambda code: SimpleNamespace(verified=True, method="self_test_passed", detail=""))
        monkeypatch.setattr(orch_mod, "save_skill", lambda **kw: skill_file)

        _install_fake_brain(monkeypatch, [
            _reply(raw_text="1. write reverse()\n2. add self-test"),  # planner
            _reply(code="def reverse(s): return s[::-1]\nassert reverse('ab') == 'ba'\nprint('OK')"),  # coder
        ])

        out = run_orchestrated("reverse a string")
        assert out.success is True
        assert out.rounds == 1
        assert out.skill_path == "reverse_a_string.md"


class TestReviewerRetry:
    def test_failed_first_round_retries_and_second_round_succeeds(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(orch_mod, "SKILLS_ROOT", tmp_path)
        skill_file = tmp_path / "thing.md"

        results = iter([
            SimpleNamespace(ok=False, stdout="", stderr="AssertionError: boom", returncode=1),
            SimpleNamespace(ok=True, stdout="OK", stderr="", returncode=0),
        ])
        verifies = iter([
            SimpleNamespace(verified=False, method="assertion_failed", detail="boom"),
            SimpleNamespace(verified=True, method="self_test_passed", detail=""),
        ])
        monkeypatch.setattr(orch_mod, "run_python_subprocess", lambda code: next(results))
        monkeypatch.setattr(orch_mod, "verify_skill", lambda code: next(verifies))
        monkeypatch.setattr(orch_mod, "save_skill", lambda **kw: skill_file)

        instances = _install_fake_brain(monkeypatch, [
            _reply(raw_text="plan"),                              # planner
            _reply(code="broken code"),                           # coder round 1 (fails)
            _reply(raw_text="fix: check the base case"),          # reviewer
            _reply(code="def f(): return 1\nprint('OK')"),        # coder round 2 (succeeds)
        ])

        out = run_orchestrated("do a thing", max_rounds=5)
        assert out.success is True
        assert out.rounds == 2
        # Reviewer's fix must actually reach the coder's next-round prompt.
        last_coder_prompt = instances[0].complete_calls[-1][-1]["content"]
        assert "check the base case" in last_coder_prompt


class TestNativeToolCalls:
    def test_tool_call_round_dispatches_and_continues_without_treating_it_as_missing_code(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setattr(orch_mod, "SKILLS_ROOT", tmp_path)
        skill_file = tmp_path / "thing.md"
        monkeypatch.setattr(orch_mod, "run_python_subprocess",
                            lambda code: SimpleNamespace(ok=True, stdout="OK", stderr="", returncode=0))
        monkeypatch.setattr(orch_mod, "verify_skill",
                            lambda code: SimpleNamespace(verified=True, method="self_test_passed", detail=""))
        monkeypatch.setattr(orch_mod, "save_skill", lambda **kw: skill_file)

        import sys
        import types
        fake_genesis_skills = types.ModuleType("genesis_skills")
        dispatched = []

        def fake_dispatch(name, args):
            dispatched.append((name, args))
            return "[READ_FILE: x.py]\nsome content"

        fake_genesis_skills.dispatch_tool_call = fake_dispatch
        monkeypatch.setitem(sys.modules, "genesis_skills", fake_genesis_skills)

        instances = _install_fake_brain(monkeypatch, [
            _reply(raw_text="plan"),  # planner
            _reply(raw_text="let me look first", tool_calls=[
                {"id": "call_1", "function": {"name": "READ_FILE", "arguments": '{"path": "x.py"}'}},
            ]),  # coder round 1: tool call, no code yet
            _reply(code="def f(): return 1\nprint('OK')"),  # coder round 2: real code
        ])

        out = run_orchestrated("use a tool then write code", max_rounds=5)
        assert out.success is True
        assert out.rounds == 2  # the tool-call round counted, then the real code round
        assert dispatched == [("READ_FILE", {"path": "x.py"})]
        # The tool result must have been fed back into the conversation.
        tool_messages = [m for m in instances[0].complete_calls[-1] if m.get("role") == "tool"]
        assert any("some content" in m["content"] for m in tool_messages)


class TestMaxRoundsExhausted:
    def test_gives_up_cleanly_after_max_rounds(self, monkeypatch) -> None:
        monkeypatch.setattr(orch_mod, "run_python_subprocess",
                            lambda code: SimpleNamespace(ok=False, stdout="", stderr="nope", returncode=1))
        monkeypatch.setattr(orch_mod, "verify_skill",
                            lambda code: SimpleNamespace(verified=False, method="assertion_failed", detail="nope"))

        replies = [_reply(raw_text="plan")]  # planner
        for _ in range(3):  # coder + reviewer alternating, for max_rounds=3
            replies.append(_reply(code="def f(): return 1"))
            replies.append(_reply(raw_text="try again"))
        _install_fake_brain(monkeypatch, replies)

        out = run_orchestrated("an impossible goal", max_rounds=3)
        assert out.success is False
        assert out.rounds == 3
        assert isinstance(out, OrchestratedOutcome)
