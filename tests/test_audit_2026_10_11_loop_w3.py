"""Трета вълна (2026-10-11): доказателството „пуснах / минаха тестовете“.

Прегледът на втората вълна показа, че чисто главовото разпознаване на
тестово пускане изпуска обичайни форми, че зелено пускане с Traceback в
изхода изпада, и че паднала НЕтестова команда „доказва“ „Пуснах тестовете“.
Всеки тест тук падаше на claude/mega-upgrade-oct11 преди поправката.
"""
from __future__ import annotations

import sys

import pytest

from genesis_agent import claim_check
from genesis_agent.claim_check import is_test_run
from genesis_agent.code_check import RunCheck


def _ran(cmd: str, rc: int, out: str = ""):
    return claim_check.counts_as_executed("RUN_CMD", cmd, f"[RUN_CMD: {cmd}]  (rc={rc})\n{out}")


def _executed(*entries):
    return [e for e in entries if e]


# ── 1. Зелено пускане с Traceback в изхода пак е зелено пускане ────────────

@pytest.mark.parametrize("out", [
    ("ERROR:root:boom\nTraceback (most recent call last):\n  File \"app.py\", line 3\n"
     "ValueError: x\n.\n----\nRan 1 test in 0.001s\n\nOK"),
    "test_missing_file: файлът не съществува — очаквано\n3 passed",
])
def test_a_green_run_with_a_traceback_in_its_output_counts(out):
    entry = _ran("python -m unittest discover", 0, out)
    assert entry is not None
    text = "Пуснах тестовете — всички тестове минаха."
    assert claim_check.unsupported_claims(text, [entry]) == []
    # Текстовият път също.
    res = f"[RUN_CMD: python -m unittest discover]  (rc=0)\n{out}"
    assert claim_check.unsupported_claims(text, claim_check.executed_from_text_results([res])) == []


def test_a_traceback_still_disproves_a_non_test_command():
    entry = _ran("python solution.py", 0, "Traceback (most recent call last):\nKeyError")
    assert entry is None


# ── 2. Паднала НЕтестова команда не доказва „Пуснах тестовете“ ─────────────

@pytest.mark.parametrize("cmd", ["npm install", "python setup.py build", "cargo build",
                                 "make", "go build ./...", "python app.py"])
def test_a_failed_non_test_command_does_not_prove_the_tests_ran(cmd):
    entry = _ran(cmd, 1, "error: something broke")
    claims = claim_check.unsupported_claims("Пуснах тестовете.", _executed(entry))
    assert [c.kind for c in claims] == ["пускане на тестове"]


def test_a_failed_test_run_still_proves_the_tests_ran():
    entry = _ran("python -W error -m pytest", 1, "1 failed")
    assert claim_check.unsupported_claims("Пуснах тестовете: един пада.", [entry]) == []


# ── 3. Червено пускане в по-рядка форма не се скрива от зелен pytest ──────

_RUNNER_FORMS = [
    "python -W error -m pytest", "python -X dev -m pytest", "timeout -s KILL 600 pytest",
    "env -u FOO pytest", "coverage run -m pytest", "python -m coverage run -m pytest",
    "npm --prefix web test", "yarn --cwd web test", "pnpm --filter web test",
    "yarn workspace web test", "bash -c 'pytest -q'", "bash -c 'cd web && npm test'",
    "sh run_tests.sh", "cargo nextest run", "hatch run test", "poetry run test:unit",
    "docker compose run app pytest", "cmd /c pytest", "powershell -Command pytest",
    "bundle exec rspec", "./build.sh test", "node --test", "py -3 -m pytest",
    "python3.12 -m pytest", 'python -c "import pytest; pytest.main()"',
]


@pytest.mark.parametrize("cmd", _RUNNER_FORMS)
def test_test_runner_forms_are_recognised(cmd):
    assert is_test_run(cmd)


@pytest.mark.parametrize("cmd", _RUNNER_FORMS)
def test_a_red_run_after_a_green_pytest_disproves_all_passed(cmd):
    executed = _executed(_ran("pytest -q", 0, "3 passed"), _ran(cmd, 1, "1 failed"))
    assert claim_check.unsupported_claims("Всички тестове минаха.", executed)


# ── 4. По-късна НЕтестова команда, която споменава тестове, не скрива червено

@pytest.mark.parametrize("later", [
    "cat tests/test_x.py", "grep -rn foo tests/", "ruff check tests/", "git diff tests/",
    "python -m pip install pytest", "pip install pytest-cov", "echo tests done",
    "python -m mypy tests", "npx eslint tests", "npm install test-utils", "uv pip install pytest",
    "ls tests && cat tests/conftest.py", "sudo -u bob ls tests", "npm run lint",
])
def test_a_later_non_test_command_does_not_hide_a_red_run(later):
    assert not is_test_run(later)
    executed = _executed(_ran("pytest -q", 1, "1 failed"), _ran(later, 0))
    assert claim_check.unsupported_claims("Всички тестове минаха.", executed)


# ── 5. Многоредова команда (heredoc) запазва rc-то си ─────────────────────

def test_a_multi_line_test_command_keeps_its_rc():
    cmd = "cd proj\npytest -q"
    entry = claim_check.counts_as_executed("RUN_CMD", cmd, f"[RUN_CMD: {cmd}]  (rc=1)\n1 failed")
    assert entry is not None
    assert claim_check.unsupported_claims("Всички тестове минаха.", [entry])


# ── 6. capability_report подава командата, не целия резултат ─────────────

def test_command_from_result_reads_the_header():
    assert claim_check.command_from_result("[RUN_CMD: pytest -q]  (rc=1)\n1 failed") == "pytest -q"
    assert claim_check.command_from_result(
        "[RUN_CMD: python -c \"a=[1]; print(a[0])\"]  (rc=0)\n1") == 'python -c "a=[1]; print(a[0])"'
    assert claim_check.command_from_result("[RUN_CMD: rm -rf /]\n[SANDBOX BLOCKED] x") == ""


def test_capability_report_sees_a_green_then_red_pytest(monkeypatch):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "capability_report_w3", Path(__file__).resolve().parent.parent / "scripts"
        / "capability_report.py")
    assert spec and spec.loader
    cr = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "capability_report_w3", cr)  # dataclass-ите търсят модула си там
    spec.loader.exec_module(cr)

    results = ["[RUN_CMD: pytest -q]  (rc=0)\n3 passed",
               "[RUN_CMD: pytest -q]  (rc=1)\n1 failed",
               "[RUN_CMD: npm install]  (rc=1)\nERR"]

    def fake_loop(core, messages, on_assistant, on_tool_result):
        for r in results:
            on_tool_result("RUN_CMD", r, None)
        on_assistant("Всички тестове минаха.", "p", "m")
        return messages

    import genesis_agent.agent_core as ac
    monkeypatch.setattr(ac, "run_tool_loop", fake_loop)
    task = cr.Task(name="t", prompt="p")
    res = cr._run_one(task, core=None)
    assert "минаващи тестове" in res.simulated
    # Падналият `npm install` не е изпълнен инструмент.
    assert res.rounds == 2


# ── 7. code_check: флаг на интерпретатора преди `-m pip` ──────────────────

@pytest.mark.parametrize("command", [
    "python -W ignore -m pip install requests", "python -I -m pip install requests",
    "python -u -m pip install requests", "py -3 -X utf8 --version",
])
def test_code_check_pip_after_interpreter_flags_is_not_a_run(command):
    c = RunCheck()
    c.observe("[WRITE_FILE: /ws/solution.py] ✓ записани 120 символа")
    c.observe(f"[RUN_CMD: {command}]  (rc=0)\nok")
    assert "НЕ пуснат" in c.note()


@pytest.mark.parametrize("command", ["python -u solution.py", "python -W error solution.py",
                                     "python -X dev solution.py --version"])
def test_code_check_a_flagged_run_is_still_a_run(command):
    c = RunCheck()
    c.observe("[WRITE_FILE: /ws/solution.py] ✓ записани 120 символа")
    c.observe(f"[RUN_CMD: {command}]  (rc=0)\nok")
    assert "НЕ пуснат" not in c.note()
