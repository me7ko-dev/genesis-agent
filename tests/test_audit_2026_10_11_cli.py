"""Проверката 2026-10-11 на CLI повърхностите: `genesis fix`, `export`,
`setup`, потребителските команди и първото пускане без ключове. Всеки тест
пада без поправката си (пуснато срещу стария код)."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from genesis_agent import repo_agent

PY = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def _tc(name, args, i=0):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class _Reply:
    def __init__(self, tool_calls=None, raw_text=""):
        self.tool_calls = tool_calls
        self.raw_text = raw_text
        self.code = ""


def _brain(monkeypatch, replies, then=None):
    it = iter(replies)
    calls: list[int] = []

    class B:
        def __init__(self, **kw):
            pass

        def complete(self, *a, **kw):
            calls.append(1)
            return next(it, then or _Reply(raw_text="готово"))
    monkeypatch.setattr(repo_agent, "Brain", B)
    return calls


CALC_BAD = "def add(a, b):\n    return a - b\n"
TEST_CALC = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_agent, "CHECKPOINT_DIR", tmp_path / "cps")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "calc.py").write_text(CALC_BAD, encoding="utf-8")
    (root / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
    return root


# ── genesis fix: файл, който тестовете сами пишат, не е поправка ─────────────

def test_a_log_written_by_the_tests_is_not_a_fix(proj, monkeypatch):
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (proj / "run_tests.py").write_text(
        "import pathlib, time\npathlib.Path('last_run.log').write_text(str(time.time_ns()))\n",
        encoding="utf-8")
    said: list[str] = []
    calls = _brain(monkeypatch, [], then=_Reply(raw_text="Сменете a - b с a + b в calc.py."))
    out = repo_agent.repair(proj, "add е грешна", test_command=f'"{sys.executable}" run_tests.py',
                            max_rounds=3, on_status=said.append)
    assert out.success is False
    assert "last_run.log" not in out.files_touched
    assert any("описа промяна" in s for s in said), "моделът трябва да бъде върнат да действа"
    assert len(calls) == 3
