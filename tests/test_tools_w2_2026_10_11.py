"""Ревю на инструментите от 2026-10-11 (втора вълна): CRLF и `$` в
SEARCH_CODE, latin-1 байт, приет за cp1251, фалшиви cp1251 попадения в UTF-8
файлове, прегледът преди WRITE_FILE, `glob` с `[ ]` при rg, кодирането след
първите 32 KB, файлове само с CR. Всеки тест падаше на кода преди поправката."""
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


def _names(hits) -> list[str]:
    return sorted(os.path.basename(h.path) for h in hits)


# ── CRLF и `$` ───────────────────────────────────────────────────────────────

def test_search_dollar_anchor_matches_crlf_lines(ws, engine):
    (ws / "m.py").write_bytes(b"def f():\r\n    return 1\r\nx = 2\r\n")
    hits = repo_map.search_code(r"return 1$", ws)
    assert [(h.line, h.text) for h in hits] == [(2, "    return 1")]
