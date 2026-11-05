"""2026-11-05: разминаване в изхода на собствената проверка (code_check.MismatchCheck).

bench_fcc c365 (30.09, три пускания, три провала): скриптът на модела печаташе
„3 expected 4“ и накрая „All tests passed!“ — само print, rc=0. Моделът казваше
„готово“, claim_check мълчеше (rc=0, `python check.py` не е тестов пускач).
Сега ходът не свършва, докато моделът не сравни разминаването с условието —
веднъж на ход, като останалите проверки.
"""
from __future__ import annotations

import contextlib
import json
import sys
from collections import deque

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import agent_core, hooks
from genesis_agent.code_check import MismatchCheck, mismatch_line

# ── 1. Кой ред е разминаване ─────────────────────────────────────────────────


@pytest.mark.parametrize("output", [
    "3 expected 4\nAll tests passed!",                  # c365
    "count=3 (expected 4)",
    "case 2: 3 expected 4",
    "Test 3 expected 4, got 5",
    "expected [1, 2], got [1, 3]",
    "Expected: 'abc'\nGot: 'abd'",
    "Got: 5\nExpected: 6",
    "Expected output: 5\nOutput: 6",
    "expected:<4> but was:<3>",
    "очаквано 4, получено 3",
    "❌ case 3",
    "FAIL test_round_up",
    "Failed: 2",
    "4/5 passed",
])
def test_a_printed_mismatch_is_found(output) -> None:
    assert mismatch_line(output)


@pytest.mark.parametrize("output", [
    "4 expected 4\nAll tests passed!",
    "case 2: 4 expected 4",
    "Test 1 expected 4",                                 # номерът на теста, не стойност
    "Test 3 expected 4, got 4",
    "expected [1, 2], got [1, 2]",
    "result: 4.0 expected 4",
    "Expected: 'abc'\nGot: 'abc'",
    "Очаквано: 4\nПолучено: 4",
    "Error: expected 10 digits, got 9",                 # отказът, който проверката иска
    "ValueError: expected 10, got 9",
    "expected str, got int",
    "Input: 3\nOutput: 9",
    "✅ case 3",
    "PASS: 5 / FAIL: 0",
    "5/5 passed",
    "5 passed in 0.02s",
])
def test_matching_values_and_rejections_are_not_a_mismatch(output) -> None:
    assert mismatch_line(output) == ""


# ── 2. Кога е време за бележката ────────────────────────────────────────────

_RED = "[RUN_CMD: python check.py]  (rc=0)\n3 expected 4\nAll tests passed!"
_GREEN = "[RUN_CMD: python check.py]  (rc=0)\n4 expected 4\nAll tests passed!"


def test_a_mismatch_then_done_gets_one_note() -> None:
    check = MismatchCheck()
    check.observe(_RED)
    assert check.due("Готово — всички тестове минават.")
    note = check.note()
    assert "3 expected 4" in note and "python check.py" in note
    assert "условието" in note
    assert not check.due("Готово.")                      # веднъж на ход


def test_a_clean_rerun_of_the_same_check_clears_it() -> None:
    check = MismatchCheck()
    check.observe(_RED)
    check.observe(_GREEN)
    assert not check.due("Готово.")


def test_a_fix_and_any_clean_run_clears_it() -> None:
    check = MismatchCheck()
    check.observe(_RED)
    check.observe("[EDIT_FILE: solution.py] ✓ 1 замяна")
    check.observe("[RUN_CMD: python -c \"from solution import f; print(f(3))\"]  (rc=0)\n4")
    assert not check.due("Поправих го.")


def test_an_unrelated_clean_run_without_a_fix_does_not_clear_it() -> None:
    check = MismatchCheck()
    check.observe(_RED)
    check.observe("[RUN_CMD: python -c \"print(1)\"]  (rc=0)\n1")
    assert check.due("Готово.")


def test_an_honest_report_of_the_mismatch_is_left_alone() -> None:
    check = MismatchCheck()
    check.observe(_RED)
    assert not check.due("Проверката пада на случай 3 (3 срещу 4) — условието е двусмислено.")


def test_a_fix_that_was_never_rerun_gets_the_note_even_if_admitted() -> None:
    check = MismatchCheck()
    check.observe(_RED)
    check.observe("[EDIT_FILE: solution.py] ✓ 1 замяна")
    assert check.due("Поправих грешката в solution.py.")
    assert "не е пускана наново" in check.note()


@pytest.mark.parametrize("result", [
    "[RUN_CMD: cat README.md]  (rc=0)\n❌ Не пускай като root",
    "[RUN_CMD: grep -r FAIL logs]  (rc=0)\nlogs/a.txt:FAIL x",
    "[RUN_CMD: python check.py]  (rc=1)\n3 expected 4\nAssertionError",   # червено — вижда се
    "[RUN_CMD: python check.py]\n[SANDBOX DECLINED] …",                   # не тръгна
])
def test_only_finished_green_runs_of_code_are_read(result) -> None:
    check = MismatchCheck()
    check.observe(result)
    assert not check.due("Готово.")


# ── 3. c365 през истинския цикъл на хода ────────────────────────────────────

_CHECK = ("from solution import regions\n"
          "for grid, want in [([[1, 1], [0, 1]], 3), ([[1, 0], [0, 1]], 3), ([[1, 0, 1], [0, 1, 0]], 4)]:\n"
          "    print(regions(grid), 'expected', want)\n"
          "print('All tests passed!')\n")
# Грешно разбрано условие: брои запълванията, не регионите — третият случай е 3, не 4.
_SOLUTION = "def regions(grid):\n    return 3\n"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    monkeypatch.setattr(gta, "WORKSPACE", root)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    monkeypatch.setenv("GENESIS_ACCEPTANCE", "0")
    gs._SEEN_PATHS.clear()
    hooks.workspace = root
    yield root
    hooks.workspace = None
    gs._set_agent_scope(None)
    gs._set_cancel(None)


def _call(name, args, cid):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class _UI(gta.TurnUI):
    def __init__(self):
        self.log: list = []

    def thinking(self, label, spinner="dots"):
        return contextlib.nullcontext()

    def assistant(self, text): self.log.append(("assistant", text))
    def tool(self, name, result): self.log.append(("tool", name, result))
    def asked(self, q): self.log.append(("asked", q))
    def spinning(self, n): self.log.append(("spin", n))
    def warn(self, t): self.log.append(("warn", t))
    def info(self, t): self.log.append(("info", t))


def test_c365_print_only_check_does_not_end_the_turn_as_done(ws, monkeypatch) -> None:
    run = f'"{sys.executable}" check.py'
    replies = iter([
        ("", [_call("WRITE_FILE", {"path": "solution.py", "content": _SOLUTION}, "w1"),
              _call("WRITE_FILE", {"path": "check.py", "content": _CHECK}, "w2")]),
        ("", [_call("RUN_CMD", {"command": run}, "r1")]),
        ("Готово — всички тестове минават.", None),          # → RunCheck: „пробвай и извън примерите“
        ("Пробвах и трите случая — минават.", None),         # → тук: разминаването
        ("Прав си — случай 3 е 3 вместо 4. Ще го оправя после.", None),
    ])
    seen: list = []

    def fake(messages, tools=None):
        seen.append([dict(m) for m in messages])
        return next(replies)
    monkeypatch.setattr(gta, "ask_genesis", fake)
    ui = _UI()
    gta.run_turn(deque([{"role": "system", "content": "SYS"}], maxlen=gta._HISTORY_MAXLEN),
                 "брой регионите", ui)
    notes = [m["content"] for m in seen[-1] if m["role"] == "system"]
    mismatch = [n for n in notes if n.startswith("[проверка на изхода]")]
    assert mismatch, notes
    assert "3 expected 4" in mismatch[0]
    assert any("разминаване" in w for _, *rest in ui.log if _ == "warn" for w in rest)


def test_the_core_loop_reads_the_mismatch_too(tmp_path) -> None:
    replies = iter([
        ("", [_call("RUN_CMD", {"command": "python check.py"}, "r1")], "fake", "m"),
        ("Готово.", None, "fake", "m"),
        ("Сверих: 3 срещу 4 — грешното е очакването в проверката.", None, "fake", "m"),
    ])

    class _Skills:
        def dispatch_tool_call(self, name, args):
            return _RED

        def parse_and_execute_tools(self, text):
            return []

    class _Core:
        skills = _Skills()

        def __init__(self):
            self.seen: list = []

        def complete(self, messages):
            self.seen.append([dict(m) for m in messages])
            return next(replies)

        def remember(self, *a):
            pass

    core = _Core()
    agent_core.run_tool_loop(core, [{"role": "system", "content": "SYS"},
                                    {"role": "user", "content": "провери"}],
                             on_assistant=lambda *a: None, on_tool_result=lambda *a: None)
    assert any(m["role"] == "system" and m["content"].startswith("[проверка на изхода]")
               for m in core.seen[-1])
