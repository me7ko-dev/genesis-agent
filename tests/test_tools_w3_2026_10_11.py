"""Ревю на инструментите от 2026-10-11 (трета вълна): файл само с CR минава
READ→WRITE байт по байт, самотен сурогат в WRITE_FILE е отказ на български,
BOM в прегледа преди WRITE_FILE, `\\r` в SEARCH_CODE, latin-1 файл се
редактира, `./` в `glob` при rg. Всеки тест падаше на кода преди поправката,
освен [python] варианта на теста за `./`: там бъгът беше в rg, а Python пътят
е мярката, с която rg трябва да съвпадне."""
from __future__ import annotations

import pytest

import genesis_skills as gs
from genesis_agent import repo_map


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    gs.set_workspace(tmp_path)
    gs._SEEN_PATHS.clear()
    monkeypatch.setattr("genesis_agent.sandbox._POLICY", gs.sandbox.SandboxPolicy())
    yield tmp_path
    gs.set_workspace(gs._PROJECT_ROOT)


@pytest.fixture(params=["rg", "python"])
def engine(request, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(repo_map.shutil, "which", lambda _name: None)
    elif not repo_map.shutil.which("rg"):
        pytest.skip("няма rg")
    return request.param


def _body(out: str) -> str:
    return out.split("\n", 1)[1]


def _changed(diff: str) -> list[str]:
    return [ln for ln in diff.splitlines()
            if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]


# ── файл само с CR ───────────────────────────────────────────────────────────

def test_cr_only_file_round_trips_read_write_unchanged(ws):
    p = ws / "old.txt"
    p.write_bytes(b"one\rtwo\rthree\r")
    body = _body(gs._tool_read_file("old.txt"))
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "old.txt", "content": body})
    assert "✓" in out, out
    assert p.read_bytes() == b"one\rtwo\rthree\r"


def test_cr_only_file_is_one_line_in_every_view(ws):
    from genesis_agent import agent_core
    (ws / "old.txt").write_bytes(b"one\rtwo\rthree\r")
    body = _body(gs._tool_read_file("old.txt"))
    assert body.count("\n") == 0
    assert "Файлът има 1 реда" in gs._tool_read_file("old.txt", offset=2)
    assert [h.line for h in repo_map.search_code("two", ws)] == [1]
    # Прегледът не показва промяна там, където записът не прави такава.
    assert agent_core._diff_for_write(gs, {"path": "old.txt", "content": body}) is None
