"""Ревю на инструментите от 2026-10-11 (трета вълна): файл само с CR минава
READ→WRITE байт по байт, самотен сурогат в WRITE_FILE е отказ на български,
BOM в прегледа преди WRITE_FILE, `\\r` в SEARCH_CODE, latin-1 файл се
редактира, `./` в `glob` при rg. Всеки тест падаше на кода преди поправката,
освен [python] варианта на теста за `./`: там бъгът беше в rg, а Python пътят
е мярката, с която rg трябва да съвпадне."""
from __future__ import annotations

import os

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


# ── самотен сурогат ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("existing", [None, "utf-8", "cp1251"])
def test_write_with_lone_surrogate_is_refused_in_bulgarian(ws, existing):
    p = ws / "m.txt"
    if existing:
        p.write_bytes("Здравей\n".encode(existing))
        gs._tool_read_file("m.txt")
    before = p.read_bytes() if existing else None
    # Половин емоджи (high surrogate) от JSON — няма го в никое кодиране.
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "m.txt", "content": "ok \ud83d\n"})
    assert out.startswith("[WRITE_FILE"), out
    assert "❌" in out and "codec" not in out, out
    assert (p.read_bytes() if existing else p.exists()) == (before if existing else False)


# ── BOM в прегледа ───────────────────────────────────────────────────────────

def test_write_preview_of_bom_file_shows_only_the_changed_line(ws):
    from genesis_agent import agent_core
    (ws / "b.txt").write_bytes("﻿a = 1\nb = 1\n".encode())
    body = _body(gs._tool_read_file("b.txt"))
    assert body.startswith("﻿")
    for content in (body, body.removeprefix("﻿")):
        diff = agent_core._diff_for_write(gs, {"path": "b.txt",
                                               "content": content.replace("b = 1", "b = 2")})
        assert diff is not None
        assert _changed(diff) == ["-b = 1", "+b = 2"], diff


# ── `\r` в SEARCH_CODE ───────────────────────────────────────────────────────

def test_search_finds_carriage_returns(ws, engine):
    (ws / "win.bat").write_bytes(b"@echo off\r\nexit /b 0\r\n")
    (ws / "unix.sh").write_bytes(b"echo hi\nexit 0\n")
    hits = repo_map.search_code(r"\r$", ws)
    assert sorted({os.path.basename(h.path) for h in hits}) == ["win.bat"]
    assert [h.line for h in hits] == [1, 2]
    # `$` пред CRLF продължава да работи (вълна 2).
    assert [h.line for h in repo_map.search_code(r"off$", ws)] == [1]


# ── `./` в `glob` ────────────────────────────────────────────────────────────

def test_search_glob_with_dot_slash_matches_like_python(ws, engine):
    (ws / "src").mkdir()
    (ws / "src" / "a.py").write_text("needle\n", encoding="utf-8")
    (ws / "b.py").write_text("needle\n", encoding="utf-8")
    for glob in ("./src/*.py", "src/./*.py"):
        hits = repo_map.search_code("needle", ws, glob=glob)
        assert [os.path.basename(h.path) for h in hits] == ["a.py"], glob
