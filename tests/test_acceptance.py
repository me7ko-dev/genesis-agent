"""genesis_agent.acceptance — tests from the request alone (NEXT_STEPS Б.4)."""
from __future__ import annotations

from pathlib import Path

import pytest

from genesis_agent import acceptance


class _Reply:
    def __init__(self, raw: str) -> None:
        self.raw_text, self.code = raw, ""


def _fake(raw: str, seen: list | None = None):
    def complete(messages):
        if seen is not None:
            seen.append(messages[0]["content"])
        return _Reply(raw)
    return complete


_GOOD = "```python\nfrom calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n```"
_BAD = "```python\nfrom calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 6\n```"


@pytest.fixture
def ws(tmp_path) -> Path:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return tmp_path


def _check(ws, raw, **kw):
    c = acceptance.AcceptanceCheck("Направи calc.py с add(a, b)", ws, complete=_fake(raw, kw.pop("seen", None)),
                                   on=True, **kw)
    c.observe(f"[WRITE_FILE: {ws / 'calc.py'}] ✓ записани 30 символа")
    return c


def test_off_unless_enabled(ws, monkeypatch) -> None:
    monkeypatch.delenv("GENESIS_ACCEPTANCE", raising=False)
    c = acceptance.AcceptanceCheck("x", ws, complete=_fake(_GOOD))
    c.observe(f"[WRITE_FILE: {ws / 'calc.py'}] ✓ записани 30 символа")
    assert not c.due()
    monkeypatch.setenv("GENESIS_ACCEPTANCE", "1")
    assert acceptance.enabled()


def test_due_only_after_a_non_test_python_file(ws) -> None:
    c = acceptance.AcceptanceCheck("x", ws, complete=_fake(_GOOD), on=True)
    c.observe(f"[WRITE_FILE: {ws / 'tests' / 'test_calc.py'}] ✓ записани 3 символа")
    c.observe(f"[WRITE_FILE: {ws / 'index.html'}] ✓ записани 3 символа")
    assert not c.due()
    c.observe(f"[EDIT_FILE: {ws / 'calc.py'}] ✓ заменено")
    assert c.due()


def test_passing_tests_give_a_line_and_no_note(ws) -> None:
    c = _check(ws, _GOOD)
    note, line = c.check()
    assert note == ""
    assert "✓" in line and "1 passed" in line
    assert c.passed is True
    assert not c.due()                      # веднъж на ход


def test_failing_tests_go_back_with_the_rule_for_settling_them(ws) -> None:
    c = _check(ws, _BAD)
    note, line = c.check()
    assert c.passed is False and "✗" in line
    assert "САМО от заявката" in note and "УСЛОВИЕТО" in note
    assert "assert 5 == 6" in note
    assert not (ws / "test_acceptance.py").exists()      # проектът не е пипан
    assert c.test_file is not None and c.test_file.is_file()


def test_the_writer_sees_the_request_and_file_names_never_the_code(ws) -> None:
    seen: list[str] = []
    c = _check(ws, _GOOD, rules="ЕГН: 9-та цифра четна = мъж", seen=seen)
    c.check()
    prompt = seen[0]
    assert "Направи calc.py с add(a, b)" in prompt
    assert "calc.py" in prompt
    assert "return a + b" not in prompt
    assert "9-та цифра четна = мъж" in prompt


@pytest.mark.parametrize("raw", ["SKIP", "Error: цялата верига е изчерпана", "no code here"])
def test_nothing_to_test_is_skipped_quietly(ws, raw) -> None:
    note, line = _check(ws, raw).check()
    assert note == "" and "пропуснати" in line


def test_a_crashing_model_never_stops_the_turn(ws) -> None:
    def boom(messages):
        raise RuntimeError("down")
    c = acceptance.AcceptanceCheck("x", ws, complete=boom, on=True)
    c.observe(f"[WRITE_FILE: {ws / 'calc.py'}] ✓ записани 30 символа")
    assert c.check() == ("", "приемни тестове: пропуснати (down)")
