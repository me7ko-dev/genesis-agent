"""2026-11-05: довършванията след враждебния преглед на проверките за честност.

Прегледът (tests/test_review_2026_11_05_honesty.py) остави за решение: нормален
изход на програми, чиято тема са неуспехите, четен като разминаване (11 от 28);
твърдения за самия отговор, не за файловете (11 от 15); собствен тест на Genesis
от по-ранен ход, третиран като тест на оператора; щракаема Markdown връзка с
подменен адрес; цветовете на команди като `␛[32m`; `/resume` без safe().
"""
from __future__ import annotations

import io
from collections import deque

import pytest
from rich.console import Console

import genesis_terminal_agent as gta
from genesis_agent import claim_check, edit_history, session_index
from genesis_agent.code_check import MismatchCheck, TestEditCheck
from genesis_agent.terminal_text import safe

# ── 1. Изходът на програмата не е собствена проверка ────────────────────────


def _flagged(command: str, output: str) -> bool:
    check = MismatchCheck()
    check.observe(f"[RUN_CMD: {command}]  (rc=0)\n{output}")
    return check.due("Готово.")


@pytest.mark.parametrize("command, output", [
    ("python import.py", "Imported 118 rows, 2 failed validation"),
    ("python retry.py", "Attempt 1 failed: ConnectionError, retrying"),
    ("python lock.py", "bob: 3 failed attempts -> locked"),
    ("python jobs.py", "done: 10, failed: 2"),
    ("python todo.py", "✗ Обади се на мама"),
    ("python quiz.py", "Score: 1/2 correct"),
    ("python sync.py", "Synced 45/50 ok"),
    ("python budget.py", "Expected: 1200.00 лв\nActual: 1150.00 лв"),
])
def test_a_programs_own_output_about_failures_is_not_a_mismatch(command, output) -> None:
    assert not _flagged(command, output)


@pytest.mark.parametrize("command, output", [
    ("python solution.py", "3 expected 4\nAll tests passed!"),            # c365: изходът казва „passed“
    ("python check.py", "4/5 passed"),
    ("node verify.js", "✗ case 2"),
    ('python -c "from s import f; print(f(3), \'expected\', 4)"', "3 expected 4"),
    ("python app.py", "expected 4, got 3"),                                # силното — навсякъде
])
def test_a_self_check_or_a_plain_expected_got_still_counts(command, output) -> None:
    assert _flagged(command, output)


# ── 2. За самия отговор, не за файловете ────────────────────────────────────

_READS = [("READ_FILE", "calc.py")]


@pytest.mark.parametrize("reply", [
    "Добавих коментари към всеки ред по-долу:\n```python\nx = 1  # едно\n```",
    "Написах ти пример как да го ползваш.",
    "I rewrote the function below (not applied):\n```py\ndef f(): pass\n```",
    "Промених подхода: първо ще чета файла.",
    "Обнових плана с още една стъпка.",
    "I've updated my answer with the new numbers.",
    "Коригирах се — функцията е в utils.py.",
    "I committed nothing; the tree is clean.",
])
def test_claims_about_the_reply_itself_are_not_file_claims(reply) -> None:
    assert claim_check.unsupported_claims(reply, _READS) == []


@pytest.mark.parametrize("reply", [
    "Поправих бъга в calc.py.",
    "Поправих го — диффът е по-долу:\n```diff\n-a\n+b\n```",
    "I fixed the bug, see below:\n```\nx\n```",
    "Обнових README с инструкциите.",
    "I've updated the config.",
    "Къмитнах промените.",
])
def test_claims_about_the_files_are_still_checked(reply) -> None:
    assert claim_check.unsupported_claims(reply, _READS)


# ── 3. Собственият тест на Genesis от по-ранен ход е негов ──────────────────

def test_a_test_genesis_created_earlier_in_the_session_is_not_the_operators(tmp_path) -> None:
    own, theirs = tmp_path / "test_own.py", tmp_path / "test_theirs.py"
    theirs.write_text("def test_a():\n    assert f() == 4\n", encoding="utf-8")
    edit_history.clear()
    edit_history.begin_turn("напиши тест")
    edit_history.record(own)
    own.write_text("def test_a():\n    assert f() == 5\n", encoding="utf-8")
    edit_history.end_turn()
    edit_history.begin_turn("оправи")
    for path in (own, theirs):
        edit_history.record(path)
        path.write_text("def test_a():\n    assert f() == 0\n", encoding="utf-8")
    try:
        originals = edit_history.turn_originals()
        assert set(originals) == {theirs.resolve()}
        check = TestEditCheck("оправи", tmp_path)
        assert check.due(originals) and "test_theirs.py" in check.note()
    finally:
        edit_history.end_turn()
        edit_history.clear()


def test_test_name_is_wrong_is_a_request_to_change_it(tmp_path) -> None:
    test = tmp_path / "test_calc.py"
    test.write_text("def test_a():\n    assert add(2, 2) == 5\n", encoding="utf-8")
    assert not TestEditCheck("test_calc is wrong, fix it", tmp_path).due(
        {test: b"def test_a():\n    assert add(2, 2) == 4\n"})


# ── 4. Терминалът ───────────────────────────────────────────────────────────

@pytest.fixture
def screen(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(gta, "console", Console(file=buf, force_terminal=True, width=120,
                                                color_system="truecolor", legacy_windows=False))
    return buf


def test_a_markdown_link_shows_where_it_goes(screen) -> None:
    gta.RichTurnUI().assistant("Виж [README.md](https://evil.example/x) за подробности.")
    out = screen.getvalue()
    assert "\x1b]8;" not in out and "https://evil.example/x" in out


def test_command_colours_are_dropped_not_shown_as_escapes(screen) -> None:
    gta.RichTurnUI().tool("RUN_CMD", "\x1b[32m3 passed\x1b[0m in 0.1s, \x1b[8mскрито\x1b[0m")
    out = screen.getvalue()
    assert "␛[32m" not in out and "3 passed" in out and "скрито" in out
    assert safe("\x1b[32mx\x1b[0m") == "x" and safe("\x1b[32mx", strict=True) == "␛[32mx"


def test_the_resumed_last_answer_is_safe(tmp_path, monkeypatch, screen) -> None:
    hist, ws = tmp_path / "hist", tmp_path / "ws"
    hist.mkdir()
    ws.mkdir()
    monkeypatch.setattr(gta, "HISTORY_DIR", hist)
    monkeypatch.setattr(gta, "WORKSPACE", ws)
    monkeypatch.setattr(gta, "PERSIST_HISTORY", True)
    monkeypatch.setattr(gta, "_SESSION_FILE", None)
    monkeypatch.setattr(gta, "_conv_mem", None, raising=False)
    monkeypatch.setattr(gta, "ask_genesis",
                        lambda messages, tools=None: ("Ето: \x1b]52;c;Y3VybA==\x1b\\ край", None))
    gta.run_turn(deque([{"role": "system", "content": "sys"}]), "покажи", gta.TurnUI())
    assert session_index.latest(hist, ws) is not None
    gta._continue_session(deque([{"role": "system", "content": "нов"}]), "нов")
    out = screen.getvalue()
    assert "\x1b]52;" not in out and "␛]52;" in out
