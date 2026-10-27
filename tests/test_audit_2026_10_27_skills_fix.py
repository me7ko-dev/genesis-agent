"""Одит 2026-10-27 (пети кръг: `genesis fix`, verifier, доставка на умения,
/update): „✅ Поправено“ с код 0, когато моделът е преименувал, обвил или
изтрил падащия тест, без да пипне реда с assert; „червена“ сюита, която е
грешка на самия runner (няма pytest), после „поправена“ с козметична
редакция; self-test, който никога не се изпълнява (зад --test / env), или
pytest.main/unittest.main(exit=False), чийто провал излиза с код 0;
промяна само в индекса (not_with, min_score) на доставено умение, която
никога не стига до оператора; заглавие на commit с `[/…]`, което събаря
/update като rich markup."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from genesis_agent import repo_agent

_PYTEST_CMD = f'"{sys.executable}" -m pytest -q'

_CALC = "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n"
_TEST = ("from calc import add, sub\n\n\ndef test_add():\n    assert add(2, 2) == 4\n\n\n"
         "def test_sub():\n    assert sub(5, 2) == 3\n")
_COSMETIC = {"path": "calc.py", "old": "def add(a, b):", "new": "def add(a, b):  # reviewed"}


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _scripted_brain(rounds: list[list[tuple[str, dict]]]):
    """Рунд е списък от (tool, args); callable вместо списък — странична стъпка
    на „модела“ (напр. изтриване на файл без обвивка)."""
    step = {"n": 0}

    class _Brain:
        def __init__(self, *a, **k) -> None:
            pass

        def complete(self, messages, tools=None):
            step["n"] += 1
            if step["n"] > len(rounds):
                return type("R", (), {"raw_text": "Готово.", "tool_calls": None})()
            this = rounds[step["n"] - 1]
            if callable(this):
                this()
                return type("R", (), {"raw_text": "Махнах го.", "tool_calls": None})()
            calls = [{"id": str(i), "function": {"name": name, "arguments": json.dumps(args)}}
                     for i, (name, args) in enumerate(this)]
            return type("R", (), {"raw_text": "", "tool_calls": calls})()

    return _Brain


def _repair(tmp_path, monkeypatch, files, rounds, test_command=_PYTEST_CMD):
    monkeypatch.setattr(repo_agent, "CHECKPOINT_DIR", tmp_path / "checkpoints")
    root = _write(tmp_path / "proj", files)
    monkeypatch.setattr(repo_agent, "Brain", _scripted_brain(rounds))
    out = repo_agent.repair(str(root), "test_add пада", test_command=test_command,
                            max_rounds=6, on_status=lambda m: None)
    return root, out


_BASE = {"calc.py": _CALC, "tests/test_calc.py": _TEST,
         "pyproject.toml": "[project]\nname='p'\nversion='0'\n"}
_READ = [("READ_FILE", {"path": "tests/test_calc.py"}), ("READ_FILE", {"path": "calc.py"})]


# ── 1: тестът е обезвреден без да се пипа редът с assert ─────────────────────

@pytest.mark.parametrize("edit", [
    {"path": "tests/test_calc.py", "old": "def test_add():", "new": "def check_add():"},
    {"path": "tests/test_calc.py", "old": "    assert add(2, 2) == 4",
     "new": "    try:\n        assert add(2, 2) == 4\n    except AssertionError:\n        pass"},
    {"path": "tests/test_calc.py", "old": "def test_add():\n", "new": "def test_add():\n    return\n"},
], ids=["renamed", "wrapped-in-try", "early-return"])
def test_neutralising_the_failing_test_is_not_a_fix(tmp_path, monkeypatch, edit) -> None:
    _, out = _repair(tmp_path, monkeypatch, _BASE, [_READ, [("EDIT_FILE", edit), ("EDIT_FILE", _COSMETIC)]])
    assert out.tests_after is not None and out.tests_after.passed
    assert out.success is False, out.summary
    assert "Поправено:" not in out.summary


@pytest.mark.parametrize("through_shell", [
    False,
    pytest.param(True, marks=pytest.mark.skipif(os.name == "nt", reason="`rm` е само в bash")),
], ids=["direct", "run-cmd-rm"])
def test_deleting_the_failing_test_file_is_not_a_fix(tmp_path, monkeypatch, through_shell) -> None:
    files = {**_BASE, "tests/test_other.py": "def test_ok():\n    assert 1 == 1\n"}
    root = tmp_path / "proj"
    # На Windows CI RUN_CMD минава през Git Bash (`del` не съществува там), а на
    # лаптопа без него — през cmd.exe (без `rm`): затова изтриването без обвивка
    # е основният случай, а `rm` през sandbox-а — само на POSIX.
    delete = ([("RUN_CMD", {"command": "rm tests/test_calc.py"})] if through_shell
              else (lambda: (root / "tests/test_calc.py").unlink()))
    _, out = _repair(tmp_path, monkeypatch, files, [
        [("READ_FILE", {"path": "calc.py"})], delete, [("EDIT_FILE", _COSMETIC)]])
    assert not (root / "tests/test_calc.py").exists()
    assert out.success is False, out.summary
    assert "tests/test_calc.py" in out.files_touched, "изтритият файл трябва да е в диффа"


def test_a_real_fix_with_the_bookkeeping_is_still_a_fix(tmp_path, monkeypatch) -> None:
    fix = {"path": "calc.py", "old": "return a - b\n\n", "new": "return a + b\n\n"}
    _, out = _repair(tmp_path, monkeypatch, _BASE, [_READ, [("EDIT_FILE", fix)]])
    assert out.success is True, out.summary
    assert out.tests_before.failed == frozenset({"tests/test_calc.py::test_add"})
    assert out.tests_before.count == 2 and out.tests_after.count == 2


# ── 2: грешка на runner-а не е червена сюита ─────────────────────────────────

@pytest.mark.parametrize("rc, out_text", [
    (1, "/proj/.venv/bin/python: No module named pytest"),
    (127, "sh: 1: pytest: not found"),
    (9009, "'pytest' is not recognized as an internal or external command"),
    (4, "ERROR: usage: pytest [options]\npytest: error: unrecognized arguments: --bogus"),
], ids=["no-module", "127", "9009", "usage-error"])
def test_a_runner_error_is_not_a_failing_suite(tmp_path, monkeypatch, rc, out_text) -> None:
    from genesis_agent import sandbox

    class _Res:
        blocked = False
        stdout = out_text
        stderr = ""
        returncode = rc

    monkeypatch.setattr(sandbox, "run_shell", lambda *a, **k: _Res())
    run = repo_agent.run_tests(tmp_path, "python -m pytest -q")
    assert run.ran is False and run.passed is False
    assert "не тръгна" in run.output


def test_a_failing_test_that_mentions_a_missing_command_is_still_a_failing_suite(
        tmp_path, monkeypatch) -> None:
    from genesis_agent import sandbox

    class _Res:
        blocked = False
        stdout = ("F\nsh: 1: foo: not found\nFAILED tests/test_cli.py::test_foo - CalledProcessError\n"
                  "1 failed in 0.03s\n")
        stderr = ""
        returncode = 1

    monkeypatch.setattr(sandbox, "run_shell", lambda *a, **k: _Res())
    run = repo_agent.run_tests(tmp_path, "python -m pytest -q")
    assert run.ran is True and run.passed is False
    assert run.failed == frozenset({"tests/test_cli.py::test_foo"})


def test_pytest_bookkeeping_is_parsed_from_the_output(tmp_path, monkeypatch) -> None:
    from genesis_agent import sandbox

    class _Res:
        blocked = False
        stdout = ("F.\n" "FAILED tests/test_calc.py::test_add - assert -1 == 4\n"
                  "ERROR tests/test_x.py::test_y - boom\n"
                  "1 failed, 3 passed, 1 skipped, 1 error in 0.02s\n")
        stderr = ""
        returncode = 1

    monkeypatch.setattr(sandbox, "run_shell", lambda *a, **k: _Res())
    run = repo_agent.run_tests(tmp_path, "python -m pytest -q")
    assert run.ran and not run.passed
    assert run.failed == frozenset({"tests/test_calc.py::test_add", "tests/test_x.py::test_y"})
    assert run.count == 5


# ── 3: verifier — self-test, който не се изпълнява, и погълнат провал ────────

_BROKEN = "def add(a, b):\n    return a - b\n"


@pytest.mark.parametrize("tail", [
    ("import sys\n\ndef _t():\n    assert add(2, 3) == 5\n\nif __name__ == '__main__':\n"
     "    if '--test' in sys.argv:\n        _t()\n    print('OK')\n"),
    ("import os\n\ndef _t():\n    assert add(2, 3) == 5\n\nif os.environ.get('RUN_SELFTEST'):\n"
     "    _t()\nprint('OK')\n"),
], ids=["argv-gated", "env-gated"])
def test_a_self_test_that_never_runs_is_not_self_test_passed(tail) -> None:
    from genesis_agent.verifier import verify_skill
    r = verify_skill(_BROKEN + tail)
    assert r.method != "self_test_passed", r


@pytest.mark.parametrize("tail", [
    ("def test_add():\n    assert add(2, 3) == 5\n\nif __name__ == '__main__':\n"
     "    import pytest\n    pytest.main([__file__, '-q', '-p', 'no:cacheprovider'])\n    print('OK')\n"),
    ("import unittest\n\nclass T(unittest.TestCase):\n    def test_add(self):\n"
     "        self.assertEqual(add(2, 3), 5)\n\nif __name__ == '__main__':\n"
     "    unittest.main(exit=False)\n    print('OK')\n"),
    ("import sys\nprint('OK')\ntry:\n    assert add(2, 3) == 5\nexcept AssertionError:\n"
     "    sys.exit(int('0'))\n"),
], ids=["pytest.main", "unittest-exit-false", "swallowed-exit-0"])
def test_a_swallowed_test_failure_is_a_failure(tail) -> None:
    from genesis_agent.verifier import verify_skill
    r = verify_skill(_BROKEN + tail)
    assert r.verified is False, r
    assert r.method == "runtime_err"


def test_a_good_self_test_still_passes_and_a_conditional_raise_counts() -> None:
    from genesis_agent.verifier import verify_skill
    good = "def add(a, b):\n    return a + b\n"
    assert verify_skill(good + "assert add(2, 3) == 5\nprint('OK')\n").method == "self_test_passed"
    assert verify_skill(good + "if add(2, 3) != 5:\n    raise ValueError('bad')\nprint('OK')\n"
                        ).method == "self_test_passed"
    assert verify_skill(good + "class T:\n    def run(self):\n        assert add(2, 3) == 5\n"
                        "T().run()\nprint('OK')\n").method == "self_test_passed"
    # Грешка при изпълнение — съобщението ѝ остава в detail и след инструментирането.
    r = verify_skill(good + "assert add(2, 3) == 5\nimport nonexistent_pkg_xyz\nprint('OK')\n")
    assert r.method == "runtime_err" and "nonexistent_pkg_xyz" in r.detail


# ── 4: промяна само в индекса на доставено умение ────────────────────────────

def test_an_index_only_change_reaches_an_untouched_copy(tmp_path) -> None:
    from genesis_agent.config import seed_user_skills

    def shipped(not_with):
        d = tmp_path / f"shipped_{not_with or 'none'}"
        d.mkdir(exist_ok=True)
        (d / "web.md").write_text("---\nname: web\n---\n\n```python\nprint('w')\n```\n",
                                  encoding="utf-8")
        entry = {"name": "web", "file_path": "skills/web.md", "description": "web",
                 "triggers": ["сайт"], "min_score": 1, "verified": True}
        if not_with:
            entry["not_with"] = [not_with]
        (d / "skills.json").write_text(json.dumps({"version": "1.0", "skills": [entry]}),
                                       encoding="utf-8")
        return d

    user = tmp_path / "home" / "skills"
    assert seed_user_skills(shipped(None), user) == 1
    index_path = user / "skills.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["skills"][0]["signature"] = "operator-signed"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    assert seed_user_skills(shipped("scraper"), user) == 1   # същият .md, нов индекс
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["skills"][0].get("not_with") == ["scraper"]
    assert index["skills"][0].get("signature") == "operator-signed"
    assert seed_user_skills(shipped("scraper"), user) == 0   # нищо ново — нищо не се пише


# ── 5: `[/…]` в заглавие на commit не събаря /update ─────────────────────────

def test_a_closing_tag_in_a_commit_subject_does_not_break_the_update_report(
        tmp_path, monkeypatch) -> None:
    from rich.console import Console

    from genesis_agent import self_update as su
    monkeypatch.setattr(su, "_state_path", lambda: tmp_path / "update_state.json")
    su._write_state(ok=False, spec="x", error="pip: [/update] failed [notice] new release")
    text = su.report_pending()
    assert text is not None
    con = Console(record=True, width=200)
    con.print(text)                                   # преди: MarkupError
    assert "[notice] new release" in con.export_text()
