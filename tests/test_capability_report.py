"""scripts/capability_report.py — отчетът върви през същия ход като чата.

Пробата е с подготвен Brain, без мрежа: дали резултатът на задачата е
сглобен вярно — кои инструменти, кой доставчик, симулирано ли е, има ли
доказателство на диска.
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

import genesis_skills

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("capability_report", ROOT / "scripts" / "capability_report.py")
cr = importlib.util.module_from_spec(_spec)
sys.modules["capability_report"] = cr  # @dataclass търси модула си там
_spec.loader.exec_module(cr)


class _Brain:
    replies: ClassVar[list] = []

    def __init__(self, **kw) -> None:
        self.current = {"provider": "groq", "model": "gpt-oss"}

    def complete(self, messages, tools=None):
        text, calls = _Brain.replies.pop(0) if _Brain.replies else ("Край.", None)
        return SimpleNamespace(raw_text=text, tool_calls=calls)


def _call(name: str, arguments: str) -> list[dict]:
    return [{"id": "1", "function": {"name": name, "arguments": arguments}}]


@pytest.fixture
def brain(monkeypatch):
    monkeypatch.setattr("genesis_agent.brain.Brain", _Brain)
    return _Brain


def test_a_task_done_with_the_expected_tool_passes(brain, monkeypatch, tmp_path) -> None:
    brain.replies = [("", _call("READ_FILE", '{"path": "pyproject.toml"}')), (">=3.10", None)]
    monkeypatch.setattr(genesis_skills, "dispatch_tool_call", lambda name, args: 'requires-python = ">=3.10"')
    res = cr._run_one(cr.TASKS[0], tmp_path)
    assert res.ok and res.tools_used == ["READ_FILE"]
    assert (res.provider, res.model) == ("groq", "gpt-oss")
    assert res.reply_tail == ">=3.10"


def test_a_text_tag_counts_under_its_own_name(brain, monkeypatch, tmp_path) -> None:
    """Старият цикъл подаваше текстовите резултати като „инструмент“ —
    отчетът не намираше READ_FILE и мареше задачата за провалена."""
    brain.replies = [("[READ_FILE: pyproject.toml]", None), (">=3.10", None)]
    monkeypatch.setattr(genesis_skills, "parse_and_execute_tools",
                        lambda text: ['[READ_FILE: pyproject.toml]\nrequires-python = ">=3.10"']
                        if "[READ_FILE" in text else [])
    res = cr._run_one(cr.TASKS[0], tmp_path)
    assert res.ok and res.tools_used == ["READ_FILE"]


def test_a_claim_with_nothing_behind_it_is_simulation(brain, tmp_path) -> None:
    write = next(t for t in cr.TASKS if t.proof)
    brain.replies = [("Създадох файла capability_probe.txt.", None)] * 3
    res = cr._run_one(write, tmp_path)
    assert not res.ok
    assert res.simulated == ["запис на файл"]
    assert res.proof_ok is False


def test_the_proof_is_read_from_the_workspace_and_removed(brain, monkeypatch, tmp_path) -> None:
    write = next(t for t in cr.TASKS if t.proof)
    probe = tmp_path / write.proof

    def _write(name, args):
        probe.write_text(write.proof_contains, encoding="utf-8")
        return f"[WRITE_FILE: {probe}] ✓ записани 16 символа"
    monkeypatch.setattr(genesis_skills, "dispatch_tool_call", _write)
    brain.replies = [("", _call("WRITE_FILE", "{}")), ("Създадох файла.", None)]
    res = cr._run_one(write, tmp_path)
    assert res.proof_ok is True and res.ok
    assert not probe.exists(), "пробният файл не остава в работната папка"


def test_a_failing_model_is_an_error_not_a_crash(monkeypatch, tmp_path) -> None:
    class _Down(_Brain):
        def complete(self, messages, tools=None):
            raise ConnectionError("няма мрежа")
    monkeypatch.setattr("genesis_agent.brain.Brain", _Down)
    res = cr._run_one(cr.TASKS[0], tmp_path)
    assert not res.ok and res.error.startswith("ConnectionError")
