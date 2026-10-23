"""Одит на проверките на кода 2026-10-22 (синтаксис, ruff, RunCheck, READ_FILE
с ключ, TODO_WRITE). Всеки тест пада без поправката си (пуснато срещу стария код),
освен пазачите test_real_runs_still_count и test_numbers_inside_text_are_kept."""
from __future__ import annotations

import ast
import json
import types

import pytest

import genesis_skills as gs
from genesis_agent import code_edit, code_validate, explore, todos
from genesis_agent.code_check import RunCheck, _runs_python

_no_ruff = not code_validate._ruff_available()


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    gs.set_workspace(tmp_path)
    gs._SEEN_PATHS.clear()
    monkeypatch.setattr("genesis_agent.sandbox._POLICY", gs.sandbox.SandboxPolicy(mode="deny"))
    todos.clear()
    yield tmp_path
    todos.clear()
    gs.set_workspace(gs._PROJECT_ROOT)


class _Completed:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout.encode("utf-8")
        self.stderr = stderr.encode("utf-8")
        self.returncode = returncode


# ── 1. Синтаксисът е на проекта, не на Python-а, на който върви Genesis ─────

@pytest.mark.skipif(_no_ruff, reason="ruff не е инсталиран")
def test_newer_python_syntax_is_not_refused_on_older_genesis(ws, monkeypatch) -> None:
    """Genesis на 3.11 и проект за 3.12: `type X = …` и `def f[T]` се отказваха
    като „чупи синтаксиса“ (мерено с python3.11). Симулираме по-стария парсер."""
    real_parse = ast.parse
    monkeypatch.setattr(code_edit.ast, "parse",
                        lambda src, *a, **kw: real_parse(src, feature_version=(3, 11)))
    app = ws / "app.py"
    app.write_text("def label(names):\n    return ', '.join(names)\n", encoding="utf-8")
    r = code_edit.edit_file(app, "def label(names):",
                            "type Names = list[str]\n\n\ndef label[T](names: Names) -> str:")
    assert r.ok, r.detail
    assert "type Names" in app.read_text(encoding="utf-8")
    # Истински счупеното още се отказва.
    r = code_edit.edit_file(app, "return ', '.join(names)", "return ', '.join(names")
    assert not r.ok and "ФАЙЛЪТ НЕ Е ПРОМЕНЕН" in r.detail
    # И WRITE_FILE не го нарича „не е валиден Python“.
    _, note = gs._python_write_check(ws / "b.py", "type Pair = tuple[int, int]\n")
    assert note == ""


# ── 2. WRITE_FILE: ruff --fix не пренаписва тихо ────────────────────────────

_IS_ONE = 'def f(x):\n    if x is 1:\n        return "one"\n    return "other"\n\n\nprint(f(1))\n'


@pytest.mark.skipif(_no_ruff, reason="ruff не е инсталиран")
@pytest.mark.filterwarnings("ignore::SyntaxWarning")
def test_write_file_exact_is_byte_for_byte_and_autofix_is_reported(ws) -> None:
    out = gs._tool_write_file("a.py", _IS_ONE, exact=True)
    assert "✓" in out, out
    assert (ws / "a.py").read_text(encoding="utf-8") == _IS_ONE
    # Без exact поправката остава, но се казва какво стана на диска.
    out = gs._tool_write_file("b.py", _IS_ONE)
    on_disk = (ws / "b.py").read_text(encoding="utf-8")
    assert "x == 1" in on_disk
    assert "Ruff поправи" in out and "x == 1" in out, out


# ── 3. Срив на ruff не е „намери проблеми“ ──────────────────────────────────

def test_ruff_crash_without_findings_fails_open(monkeypatch) -> None:
    monkeypatch.setattr(code_validate.shutil, "which", lambda _: "/usr/bin/ruff")
    monkeypatch.setattr(code_validate.subprocess, "run", lambda cmd, **kw: _Completed(
        stderr="error: Ruff crashed. thread 'main' panicked", returncode=2))
    assert code_validate.validate_code_with_ruff("print('hi')\n") == (True, "")


# ── 4. Статична проверка не е пускане ───────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "python -m py_compile solution.py", "python -m ruff check solution.py",
    "python -m mypy solution.py", "python3 -m black solution.py", "python -m compileall .",
    "python -m flake8 solution.py", "py -3.12 -m pyflakes solution.py", "python -m isort .",
    "python -m pylint solution.py",
])
def test_static_checks_do_not_count_as_running_the_code(cmd) -> None:
    assert not _runs_python(cmd)
    rc = RunCheck("задача")
    rc.observe("[WRITE_FILE: solution.py] ✓ записани 10 символа")
    rc.observe(f"[RUN_CMD: {cmd}]  (rc=0)\n")
    assert "НЕ пуснат" in rc.note()


@pytest.mark.parametrize("cmd", ["python solution.py", "python -m pytest -q",
                                 "python -m py_compile solution.py && python solution.py"])
def test_real_runs_still_count(cmd) -> None:
    assert _runs_python(cmd)


# ── 5. Частен ключ под невинно име ──────────────────────────────────────────

_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n"


def test_read_file_refuses_private_key_by_content(ws, monkeypatch) -> None:
    (ws / "id_deploy").write_text(_KEY, encoding="utf-8")
    out = gs._tool_read_file("id_deploy")
    assert "SANDBOX DENIED" in out and "b3BlbnNzaC1rZXkt" not in out, out
    # Интерактивно: пита веднъж, и след „да“ показва.
    asked: list[str] = []
    monkeypatch.setattr("genesis_agent.sandbox._POLICY", gs.sandbox.SandboxPolicy(
        mode="interactive", confirm_fn=lambda op, v: asked.append(op) or True))
    out = gs._tool_read_file("id_deploy")
    assert len(asked) == 1 and "b3BlbnNzaC1rZXkt" in out


def test_explore_does_not_show_private_key(ws) -> None:
    (ws / "id_deploy").write_text(_KEY, encoding="utf-8")
    (ws / "notes.md").write_text("# бележки\n", encoding="utf-8")
    script = ["[READ_FILE: id_deploy] [READ_FILE: notes.md]", "готово"]
    seen: list[str] = []

    def complete(messages, tools=None):
        seen.append(messages[-1]["content"])
        return types.SimpleNamespace(raw_text=script[len(seen) - 1], tool_calls=None)

    explore.explore("къде са ключовете?", str(ws), complete=complete)
    assert "# бележки" in seen[1]
    assert "b3BlbnNzaC1rZXkt" not in seen[1]


# ── 6. TODO_WRITE: номериран списък и собствените глифове ───────────────────

@pytest.mark.parametrize("arg", [
    "1. [x] прочети\n2. [~] поправи\n3. [ ] тествай",
    "☑ прочети\n▶ поправи\n☐ тествай",
    "1) ✅ прочети\n2) ▶️ поправи\n3) ☐ тествай",
    "1. [x] прочети; 2. [~] поправи; 3. [ ] тествай",
])
def test_todo_text_keeps_statuses(arg) -> None:
    out = todos.write(arg)
    assert "(1/3 готови)" in out, out
    assert [(i["content"], i["status"]) for i in todos.items()] == [
        ("прочети", "completed"), ("поправи", "in_progress"), ("тествай", "pending")]


def test_todo_render_round_trips() -> None:
    todos.write("- [x] прочети\n- [~] поправи\n- [ ] тествай")
    before = todos.items()
    shown = todos.render().split("\n", 1)[1]
    todos.clear()
    todos.write(shown, native=True)
    assert todos.items() == before


def test_numbers_inside_text_are_kept() -> None:
    todos.write("[ ] 3.5 версия\n[ ] 2) друго")
    assert [i["content"] for i in todos.items()] == ["3.5 версия", "2) друго"]


# ── 7. Самотен сурогат в .py — българското съобщение, не кодекът ────────────

def test_lone_surrogate_in_py_write_and_edit_gives_the_bulgarian_refusal(ws) -> None:
    raw = json.dumps({"path": "a.py", "content": 'MSG = "Готово \ud83d"\nprint(MSG)\n'})
    out = gs.dispatch_tool_call("WRITE_FILE", raw)
    assert "ФАЙЛЪТ НЕ Е ПРОМЕНЕН" in out and "codec" not in out, out
    assert not (ws / "a.py").exists()
    (ws / "b.py").write_text('MSG = "x"\n', encoding="utf-8")
    gs.dispatch_tool_call("READ_FILE", {"path": "b.py"})
    out = gs.dispatch_tool_call("EDIT_FILE", json.dumps({"path": "b.py", "old": '"x"',
                                                         "new": '"\ud83d"'}))
    assert "ФАЙЛЪТ НЕ Е ПРОМЕНЕН" in out and "codec" not in out, out
    assert (ws / "b.py").read_text(encoding="utf-8") == 'MSG = "x"\n'
