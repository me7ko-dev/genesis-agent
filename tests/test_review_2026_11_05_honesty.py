"""Преглед 2026-11-05 на проверките за честност на хода (шести кръг).

Фалшивите тревоги струват платено повикване и учат оператора да не им вярва.
Всеки случай тук е честен изход/отговор, който даваше бележка преди поправката:
- зелен unittest с expectedFailure („OK (expected failures=1)“), „passed=12
  failed=0“ и JSON/repr с еднакви expected/got — „[проверка на изхода]“;
- цитат на чужди думи („Ти написа: „Добавих нов endpoint““) — „Твърдиш…“;
- поискано параметризиране на тестовете — „[проверка на тестовете]“ с
  „върни теста както беше“;
- 150 KB отговор с много отрицания — квадратично време (16 s).
"""
from __future__ import annotations

import contextlib
import json
import sys
import time
from collections import deque

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import claim_check, hooks
from genesis_agent.code_check import MismatchCheck, TestEditCheck, mismatch_line

# ── 1. Зелен изход, който не е разминаване ──────────────────────────────────


@pytest.mark.parametrize("output", [
    # unittest с @expectedFailure: зелено, rc=0
    "..x\n" + "-" * 70 + "\nRan 3 tests in 0.001s\n\nOK (expected failures=1)",
    "OK (skipped=1, expected failures=2)",
    # числото пред „failed“ е на passed
    "passed=12 failed=0",
    "Passed: 12 Failed: 0",
    "Passed 5   Failed 0",
    # JSON / repr с еднакви стойности: затварящата кавичка на ключа не е стойност
    '[{"case": 1, "expected": 3, "got": 3}, {"case": 2, "expected": 4, "got": 4}]',
    '{"results": [{"expected": 1, "got": 1}], "passed": 1}',
    "results: [{'expected': 3, 'got': 3}, {'expected': 4, 'got': 4}]",
])
def test_green_summaries_and_equal_json_are_not_a_mismatch(output) -> None:
    assert mismatch_line(output) == ""


@pytest.mark.parametrize("output", [
    "passed=10 failed=2",
    "3 failed, 1 passed",
    "FAILED (failures=1, expected failures=1)",
    "expected 'abc', got 'abd'",
    "Failed: 2",
])
def test_real_mismatches_are_still_found(output) -> None:
    assert mismatch_line(output)


def test_a_green_unittest_with_an_expected_failure_does_not_get_a_note() -> None:
    check = MismatchCheck()
    check.observe("[RUN_CMD: python -m unittest]  (rc=0)\nstderr:\n..x\n" + "-" * 70
                  + "\nRan 3 tests in 0.001s\n\nOK (expected failures=1)")
    assert not check.due("Всички тестове минават.")


# ── 2. Цитат не е твърдение ─────────────────────────────────────────────────

_READ = [("READ_FILE", "calc.py")]


@pytest.mark.parametrize("reply", [
    "Ти написа: „Добавих нов endpoint“ — прегледах го и има проблем в app.py.",
    "В CHANGELOG.md пише „Поправих бъга с UTF-8“, но кодът в calc.py още е стар.",
    "You wrote “I fixed the login bug” — but calc.py still subtracts.",
    'Commit съобщението гласи: "Добавих валидация" — но validation.py го няма.',
    "Предлагам commit съобщение: «Поправих знака в add()»",
])
def test_quoted_words_are_not_the_models_claim(reply) -> None:
    assert claim_check.unsupported_claims(reply, _READ) == []


@pytest.mark.parametrize("reply", [
    'Поправих бъга в "calc.py".',
    'Функцията "add" — поправих я.',
    "Поправих бъга. Пише „OK“ навсякъде.",
])
def test_a_claim_next_to_quotes_is_still_a_claim(reply) -> None:
    assert claim_check.unsupported_claims(reply, _READ)


# ── 3. Поискано параметризиране на тестовете ────────────────────────────────


@pytest.mark.parametrize("request_", [
    "параметризирай тестовете в test_calc.py",
    "use pytest.mark.parametrize in tests/test_calc.py",
    "рефакторирай тестовете с parametrize",
    "refactor the tests to use parametrize",
])
def test_a_requested_parametrize_is_not_a_weakened_test(request_, tmp_path) -> None:
    test = tmp_path / "tests" / "test_calc.py"
    test.parent.mkdir()
    test.write_text("import pytest\n\n\n@pytest.mark.parametrize('a, b, want', [(2, 2, 4), (1, 1, 2)])\n"
                    "def test_add(a, b, want):\n    assert add(a, b) == want\n", encoding="utf-8")
    old = b"def test_add():\n    assert add(2, 2) == 4\n    assert add(1, 1) == 2\n"
    assert not TestEditCheck(request_, tmp_path).due({test: old})


def test_an_unrequested_parametrize_still_gets_the_note(tmp_path) -> None:
    test = tmp_path / "tests" / "test_calc.py"
    test.parent.mkdir()
    test.write_text("def test_add(a, b, want):\n    assert add(a, b) == want\n", encoding="utf-8")
    old = b"def test_add():\n    assert add(2, 2) == 4\n"
    assert TestEditCheck("оправи бъга, test_calc пада", tmp_path).due({test: old})


# ── 4. Дълъг отговор — линейно време ────────────────────────────────────────


def test_a_long_reply_with_many_negations_is_checked_in_linear_time() -> None:
    # Преди: 150 KB „Не промених нищо.“ — ~16 s (изречението се търсеше от началото).
    reply = "Не промених нищо. " * 8300
    started = time.monotonic()
    assert claim_check.unsupported_claims(reply, _READ) == []
    assert time.monotonic() - started < 4


# ── 5. Истинският цикъл: зелен unittest с expectedFailure ────────────────────


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


class _UI(gta.TurnUI):
    def thinking(self, label, spinner="dots"):
        return contextlib.nullcontext()

    def assistant(self, text): pass
    def tool(self, name, result): pass
    def asked(self, q): pass
    def spinning(self, n): pass
    def warn(self, t): pass
    def info(self, t): pass


def test_expected_failure_through_the_real_turn(ws, monkeypatch) -> None:
    (ws / "test_calc.py").write_text(
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertEqual(1 + 1, 2)\n\n"
        "    @unittest.expectedFailure\n    def test_known(self):\n        self.assertEqual(1, 2)\n",
        encoding="utf-8")
    call = {"id": "r1", "type": "function", "function": {
        "name": "RUN_CMD", "arguments": json.dumps({"command": f'"{sys.executable}" -m unittest'})}}
    replies = iter([("", [call]), ("Всички тестове минават.", None), ("Разбрах.", None)])
    seen: list = []

    def fake(messages, tools=None):
        seen.append([dict(m) for m in messages])
        return next(replies)
    monkeypatch.setattr(gta, "ask_genesis", fake)
    gta.run_turn(deque([{"role": "system", "content": "SYS"}], maxlen=gta._HISTORY_MAXLEN),
                 "пусни тестовете", _UI())
    tool_out = [str(m.get("content")) for m in seen[-1] if m.get("role") == "tool"]
    assert any("expected failures=1" in t for t in tool_out)
    notes = [str(m.get("content") or "") for m in seen[-1] if m.get("role") == "system"]
    assert not [n for n in notes if n.startswith("[проверка на изхода]")]
