"""Честността, отвореното от одита на 2026-11-10 (точки D и E), мерено 2026-11-15.

D. mismatch_line (изход на проверка с rc=0) не виждаше `Test 2: FAIL`, `[FAIL]`,
   `test_x ... FAILED`, `✗` в края на реда, `1 of 3 tests failed`; а брояч на
   напредъка `1/3 ok … 3/3 ok` беше тревога.
E. weakening (тест на оператора, сменен в хода) не виждаше `def test_x` →
   `def _test_x` (pytest вече не го събира), голо `return` пред проверките и
   `except AssertionError: pass`.
"""
from __future__ import annotations

import pytest

from genesis_agent.code_check import mismatch_line, weakening


@pytest.mark.parametrize("output", [
    "Test 1: PASS\nTest 2: FAIL\nTest 3: PASS",
    "[PASS] parse\n[FAIL] total\n",
    "test_add (t.TestCalc) ... ok\ntest_sub (t.TestCalc) ... FAILED",
    "tests/t.py::test_add PASSED\ntests/t.py::test_sub FAILED",
    "parse ✓\ntotal ✗\n",
    "1 of 3 tests failed",
    "2 от 5 теста паднаха",
])
def test_a_failure_in_a_check_output_is_seen(output):
    assert mismatch_line(output), output


@pytest.mark.parametrize("output", [
    "1/3 ok\n2/3 ok\n3/3 ok",
    "[1/4] building\n[4/4] done\n4/4 passed",
    "Test 2: FAILOVER configured\nAll checks passed",
    "0 of 3 tests failed",
    "FAILED: 0",
    "test_add ... ok\ntest_sub ... ok",
])
def test_progress_and_green_summaries_are_not_failures(output):
    assert mismatch_line(output) == "", output


def test_a_counter_that_never_reaches_the_end_is_still_a_failure():
    assert mismatch_line("1/3 ok\n2/3 ok")
    assert mismatch_line("3/3 passed\n1/3 passed")       # второто пускане е червено


BEFORE = """\
def test_add():
    assert add(2, 2) == 4


def test_sub():
    assert sub(3, 1) == 2
"""


@pytest.mark.parametrize("after,why", [
    (BEFORE.replace("def test_sub", "def _test_sub"), "test_sub"),
    (BEFORE.replace("def test_sub", "def disabled_test_sub"), "test_sub"),
    (BEFORE.replace("def test_add():\n", "def test_add():\n    return\n"), "return"),
    (BEFORE.replace("    assert sub(3, 1) == 2\n",
                    "    try:\n        assert sub(3, 1) == 2\n    except AssertionError:\n        pass\n"),
     "AssertionError"),
    (BEFORE.replace("    assert sub(3, 1) == 2\n",
                    "    try:\n        assert sub(3, 1) == 2\n    except Exception:\n        pass\n"),
     "except"),
])
def test_a_disabled_or_swallowed_check_is_weakening(after, why):
    reason = weakening(BEFORE, after)
    assert reason and why in reason, reason


@pytest.mark.parametrize("after", [
    BEFORE.replace("def test_sub", "def test_subtract"),          # пак се събира
    BEFORE + "\n\ndef _helper():\n    return\n",                    # помощник, не тест
    BEFORE + "\n\ndef test_raises():\n    with pytest.raises(ValueError):\n        f()\n",
    BEFORE.replace("assert add(2, 2) == 4", "assert add(2,2)==4"),
])
def test_ordinary_test_edits_are_not_weakening(after):
    assert weakening(BEFORE, after) == "", after
