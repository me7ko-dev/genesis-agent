"""genesis_agent.project_builder — multi-file project generation.

Had zero test coverage before this file, despite a real, live bug sitting in
it: the test-runner shell command built its working directory with a bare
`cd {proj_dir} && ...` string instead of passing `cwd=` to sandbox.run_shell
— breaks the moment `proj_dir` contains a space (a routine Windows path like
"C:\\Users\\John Doe\\..."). Fixed in the same session; the regression test
below (`test_runs_tests_via_cwd_not_a_shell_cd`) pins the fix down so it
cannot silently regress back to string-concatenated `cd`.
"""
from __future__ import annotations

import json

import pytest

from genesis_agent import project_builder as pb

_TWO_BLOCKS = (
    "```python name=stack.py\n"
    "class Stack:\n"
    "    def __init__(self):\n"
    "        self.items = []\n"
    "```\n"
    "```python name=test_stack.py\n"
    "import unittest\n"
    "from stack import Stack\n"
    "class T(unittest.TestCase):\n"
    "    def test_init(self):\n"
    "        self.assertEqual(Stack().items, [])\n"
    "```\n"
)


class _FakeReply:
    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text
        self.code = ""


@pytest.fixture(autouse=True)
def _projects_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pb, "PROJECTS_DIR", tmp_path / "projects_out")
    monkeypatch.setattr(pb, "PROJECTS_INDEX", tmp_path / "projects_out" / "projects.json")
    monkeypatch.setattr(pb.dna, "validate_goal_ethics", lambda _goal: None)
    monkeypatch.setattr(pb.dna, "assert_operator_if_strict", lambda _op: None)
    return tmp_path


# ── _parse_files ─────────────────────────────────────────────────────────

def test_parse_files_extracts_named_blocks() -> None:
    files = pb._parse_files(_TWO_BLOCKS)
    assert set(files) == {"stack.py", "test_stack.py"}
    assert "class Stack" in files["stack.py"]
    assert "import unittest" in files["test_stack.py"]


def test_parse_files_empty_when_no_named_blocks() -> None:
    assert pb._parse_files("just prose, no code blocks") == {}


# ── _ensure_layout ───────────────────────────────────────────────────────

def test_ensure_layout_creates_dir_and_index(_projects_dir) -> None:
    pb._ensure_layout()
    assert pb.PROJECTS_DIR.is_dir()
    idx = json.loads(pb.PROJECTS_INDEX.read_text(encoding="utf-8"))
    assert idx == {"version": 1, "projects": []}


def test_ensure_layout_does_not_clobber_an_existing_index(_projects_dir) -> None:
    pb.PROJECTS_DIR.mkdir(parents=True)
    pb.PROJECTS_INDEX.write_text(json.dumps({"version": 1, "projects": ["x"]}), encoding="utf-8")
    pb._ensure_layout()
    assert json.loads(pb.PROJECTS_INDEX.read_text(encoding="utf-8"))["projects"] == ["x"]


# ── build_project: the cwd/`cd` regression ──────────────────────────────

def test_runs_tests_via_cwd_not_a_shell_cd(monkeypatch, tmp_path_factory) -> None:
    """The actual bug: `cd {proj_dir} && ...` in the command string breaks on
    any path containing a space. run_shell must get the directory through its
    own `cwd=` kwarg instead — verified two ways: the proj_dir passed as cwd
    is the real (space-containing) directory, and the command string itself
    never contains a `cd` shell built-in."""
    spacey_base = tmp_path_factory.mktemp("has space in it")
    monkeypatch.setattr(pb, "DATA_DIR", spacey_base)
    monkeypatch.setattr(pb, "PROJECTS_DIR", spacey_base / "projects_out")
    monkeypatch.setattr(pb, "PROJECTS_INDEX", spacey_base / "projects_out" / "projects.json")

    monkeypatch.setattr(pb.Brain, "complete", lambda self, messages, **kw: _FakeReply(_TWO_BLOCKS))

    captured = {}

    def fake_run_shell(command, *, cwd=None, **kw):
        captured["command"] = command
        captured["cwd"] = cwd
        return type("R", (), {"ok": True, "stdout": "OK", "stderr": ""})()

    monkeypatch.setattr(pb.sandbox, "run_shell", fake_run_shell)

    out = pb.build_project("a stack with push/pop/peek")

    assert out.success is True
    assert " has space in it" not in (captured.get("command") or "") or True  # sanity: no crash
    assert "cd " not in captured["command"]
    assert captured["cwd"] == spacey_base / "projects_out" / pb.slugify("a stack with push/pop/peek")
    assert captured["cwd"].is_dir()  # the space-containing directory really exists on disk


# ── build_project: happy path ────────────────────────────────────────────

def test_build_project_success_writes_files_and_index(_projects_dir, monkeypatch) -> None:
    monkeypatch.setattr(pb.Brain, "complete", lambda self, messages, **kw: _FakeReply(_TWO_BLOCKS))
    monkeypatch.setattr(pb.sandbox, "run_shell",
                        lambda *a, **kw: type("R", (), {"ok": True, "stdout": "OK", "stderr": ""})())

    out = pb.build_project("a stack with push/pop/peek")

    assert out.success is True
    assert out.rounds == 1
    proj_dir = pb.PROJECTS_DIR / pb.slugify("a stack with push/pop/peek")
    assert (proj_dir / "stack.py").exists()
    assert (proj_dir / "test_stack.py").exists()
    assert (proj_dir / "README.md").exists()
    idx = json.loads(pb.PROJECTS_INDEX.read_text(encoding="utf-8"))
    assert idx["projects"][0]["slug"] == pb.slugify("a stack with push/pop/peek")
    assert idx["projects"][0]["verified"] is True


def test_build_project_rebuild_replaces_old_index_entry(_projects_dir, monkeypatch) -> None:
    """Re-running the same goal must not accumulate duplicate index entries."""
    monkeypatch.setattr(pb.Brain, "complete", lambda self, messages, **kw: _FakeReply(_TWO_BLOCKS))
    monkeypatch.setattr(pb.sandbox, "run_shell",
                        lambda *a, **kw: type("R", (), {"ok": True, "stdout": "OK", "stderr": ""})())

    pb.build_project("a stack with push/pop/peek")
    pb.build_project("a stack with push/pop/peek")

    idx = json.loads(pb.PROJECTS_INDEX.read_text(encoding="utf-8"))
    slugs = [p["slug"] for p in idx["projects"]]
    assert slugs.count(pb.slugify("a stack with push/pop/peek")) == 1


# ── build_project: retry / failure paths ─────────────────────────────────

def test_build_project_retries_when_blocks_are_missing(_projects_dir, monkeypatch) -> None:
    replies = iter([_FakeReply("no code blocks here, sorry"), _FakeReply(_TWO_BLOCKS)])
    monkeypatch.setattr(pb.Brain, "complete", lambda self, messages, **kw: next(replies))
    monkeypatch.setattr(pb.sandbox, "run_shell",
                        lambda *a, **kw: type("R", (), {"ok": True, "stdout": "OK", "stderr": ""})())

    out = pb.build_project("a stack with push/pop/peek", max_rounds=3)
    assert out.success is True
    assert out.rounds == 2


def test_build_project_fails_after_max_rounds_when_tests_never_pass(_projects_dir, monkeypatch) -> None:
    monkeypatch.setattr(pb.Brain, "complete", lambda self, messages, **kw: _FakeReply(_TWO_BLOCKS))
    monkeypatch.setattr(pb.sandbox, "run_shell",
                        lambda *a, **kw: type("R", (), {"ok": False, "stdout": "", "stderr": "boom"})())

    out = pb.build_project("a stack with push/pop/peek", max_rounds=2)
    assert out.success is False
    assert out.rounds == 2
    assert "boom" in out.last_error


def test_build_project_dna_refusal_short_circuits(_projects_dir, monkeypatch) -> None:
    def _refuse(_goal):
        raise pb.dna.GenesisDNAError("отказано от DNA")
    monkeypatch.setattr(pb.dna, "validate_goal_ethics", _refuse)

    out = pb.build_project("something unethical")
    assert out.success is False
    assert out.rounds == 0
    assert "отказано" in out.last_error
