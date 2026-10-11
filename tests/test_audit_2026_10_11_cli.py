"""Проверката 2026-10-11 на CLI повърхностите: `genesis fix`, `export`,
`setup`, потребителските команди и първото пускане без ключове. Всеки тест
пада без поправката си (пуснато срещу стария код)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile

import pytest

from genesis_agent import deliver, repo_agent

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


# ── genesis fix: подпапка на git хранилище (monorepo) ───────────────────────

def _monorepo(tmp_path):
    mono = tmp_path / "mono"
    backend = mono / "backend"
    backend.mkdir(parents=True)
    (backend / "calc.py").write_text(CALC_BAD, encoding="utf-8")
    (backend / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
    (mono / "README.md").write_text("mono\n", encoding="utf-8")
    _git(mono, "init", "-q")
    _git(mono, "add", "-A")
    _git(mono, "commit", "-qm", "init")
    return mono, backend


_HUMAN = CALC_BAD + "\n\ndef HUMAN_WORK_IN_PROGRESS():\n    pass\n"
_EDIT = _Reply([_tc("EDIT_FILE", {"path": "calc.py", "old": "a - b", "new": "a + b"})])


def test_fix_in_a_subfolder_of_a_git_repo_sees_git(proj, tmp_path, monkeypatch):
    _, backend = _monorepo(tmp_path)
    (backend / "calc.py").write_text(_HUMAN, encoding="utf-8")   # некомитната работа
    _brain(monkeypatch, [_EDIT])
    said: list[str] = []
    out = repo_agent.repair(backend, "add is wrong", test_command=PY, max_rounds=3,
                            on_status=said.append)
    assert out.success and out.checkpoint is not None, "мръсно хранилище → снимка"
    assert "+    return a + b" in out.diff
    assert "+def HUMAN_WORK_IN_PROGRESS" not in out.diff   # контекст може, добавен ред — не
    assert not any("без git" in s for s in said)


def test_clean_subfolder_fix_diff_comes_from_git(proj, tmp_path, monkeypatch):
    _, backend = _monorepo(tmp_path)
    _brain(monkeypatch, [_EDIT])
    out = repo_agent.repair(backend, "add is wrong", test_command=PY, max_rounds=3,
                            on_status=lambda m: None)
    assert out.success and out.checkpoint is None
    assert "+    return a + b" in out.diff
    assert "връщане няма" not in repo_agent._undo_hint(backend, None, ["calc.py"])


def test_fix_diff_does_not_show_human_work_in_the_same_file(proj, monkeypatch):
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    (proj / "calc.py").write_text(_HUMAN, encoding="utf-8")
    _brain(monkeypatch, [_EDIT])
    out = repo_agent.repair(proj, "fix add", test_command=PY, max_rounds=3, on_status=lambda m: None)
    assert out.success and out.checkpoint is not None
    assert "+    return a + b" in out.diff and "+def HUMAN_WORK_IN_PROGRESS" not in out.diff


# ── genesis export: каквото git пази само локално, остава локално ───────────

def _shop(tmp_path):
    root = tmp_path / "shop"
    (root / "uploads").mkdir(parents=True)
    (root / "htmlcov").mkdir()
    (root / "manage.py").write_text("print('hi')\n", encoding="utf-8")
    (root / ".gitignore").write_text("# локални\ndb.sqlite3\n*.log\nuploads/\n.coverage\nhtmlcov/\n",
                                     encoding="utf-8")
    (root / "db.sqlite3").write_bytes(b"SQLite format 3\x00 EGN 8001011234")
    (root / "server.log").write_text("user@x.bg token=abc\n", encoding="utf-8")
    (root / "uploads" / "passport_scan.jpg").write_bytes(b"\xff\xd8\xff")
    (root / ".coverage").write_bytes(b"x")
    (root / "htmlcov" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    return root


def test_export_leaves_out_what_gitignore_ignores(tmp_path):
    root = _shop(tmp_path)
    _git(root, "init", "-q")
    d = deliver.export(root, tmp_path / "out.zip", run_tests=False)
    names = sorted(n.split("/", 1)[1] for n in zipfile.ZipFile(d.zip_path).namelist())
    assert names == [".gitignore", "GENESIS_REPORT.md", "manage.py"]
    for gone in ("db.sqlite3", "server.log", "uploads/", ".coverage", "htmlcov/"):
        assert gone in d.report, gone


def test_export_reads_gitignore_without_git(tmp_path):
    root = _shop(tmp_path)
    keep, withheld = deliver.project_files(root)
    assert sorted(p.relative_to(root).as_posix() for p in keep) == [".gitignore", "manage.py"]
    assert any(w.startswith("uploads/") for w in withheld)
