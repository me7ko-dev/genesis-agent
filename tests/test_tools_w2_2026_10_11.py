"""Ревю на инструментите от 2026-10-11 (втора вълна): CRLF и `$` в
SEARCH_CODE, latin-1 байт, приет за cp1251, фалшиви cp1251 попадения в UTF-8
файлове, прегледът преди WRITE_FILE, `glob` с `[ ]` при rg, кодирането след
първите 32 KB. Всеки тест падаше на кода преди поправката,
освен [python] вариантите на test_cp1251_pass_does_not_hit_utf8_files и двата
теста за `glob`: там бъгът беше в rg, а Python пътят е мярката, с която rg
трябва да съвпадне."""
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


# ── latin-1 байт не е cp1251 ─────────────────────────────────────────────────

def test_one_latin1_letter_is_not_taken_for_cp1251(ws):
    from genesis_agent import code_edit
    raw = b"x = 'Caf\xe9'\n"
    assert code_edit.decode_text(raw)[1] == "utf-8"
    (ws / "m.py").write_bytes(raw)
    assert "й" not in gs._tool_read_file("m.py")
    # Истинската кирилица в cp1251 си остава cp1251.
    assert code_edit.decode_text("# Грешка при запис\n".encode("cp1251"))[1] == "cp1251"


def test_write_over_latin1_file_with_accented_letter_succeeds(ws):
    p = ws / "m.py"
    p.write_bytes(b"x = 'Caf\xe9'\n")
    gs._tool_read_file("m.py")
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "m.py", "content": "x = 'Café'\n"})
    assert "✓" in out, out
    assert p.read_bytes() == "x = 'Café'\n".encode()


def test_write_over_cp1251_file_with_char_outside_cp1251_becomes_utf8(ws):
    p = ws / "legacy.txt"
    p.write_bytes("Здравей\n".encode("cp1251"))
    body = _body(gs._tool_read_file("legacy.txt"))
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "legacy.txt",
                                               "content": body + "Café ✓\n"})
    assert "✓ записани" in out, out
    assert "UTF-8" in out
    assert p.read_bytes() == "Здравей\nCafé ✓\n".encode()


# ── втория проход на rg (windows-1251) ───────────────────────────────────────

def test_cp1251_pass_does_not_hit_utf8_files(ws, engine):
    # UTF-8 „мир“ (D0 BC D0 B8 D1 80) в cp1251 е „РјРёСЂ“ — главно „Р“.
    (ws / "u.txt").write_text("мир\n", encoding="utf-8")
    assert repo_map.search_code("Р", ws) == []
    (ws / "c.txt").write_bytes("Рим\n".encode("cp1251"))
    hits = repo_map.search_code("Р", ws)
    assert _names(hits) == ["c.txt"] and hits[0].text == "Рим"


# ── прегледът преди WRITE_FILE ───────────────────────────────────────────────

def test_write_preview_of_cp1251_file_shows_only_the_changed_line(ws):
    from genesis_agent import agent_core
    (ws / "legacy.txt").write_bytes("Здравей\r\nсвят\r\nname = 1\r\n".encode("cp1251"))
    body = _body(gs._tool_read_file("legacy.txt"))
    diff = agent_core._diff_for_write(gs, {"path": "legacy.txt",
                                           "content": body.replace("name = 1", "name = 2")})
    assert diff is not None
    changed = [ln for ln in diff.splitlines()
               if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    assert changed == ["-name = 1", "+name = 2"], diff


# ── `glob` с `[ ]` ───────────────────────────────────────────────────────────

def test_search_glob_with_brackets_finds_the_file(ws, engine):
    (ws / "pages").mkdir()
    (ws / "pages" / "[slug].tsx").write_text("useRouter()\n", encoding="utf-8")
    (ws / "pages" / "s.tsx").write_text("useRouter()\n", encoding="utf-8")
    hits = repo_map.search_code("useRouter", ws, glob="pages/[slug].tsx")
    # Като GLOB и Python пътя: буквалното име ИЛИ класът знаци (`s.tsx`).
    assert _names(hits) == ["[slug].tsx", "s.tsx"]


def test_search_glob_with_a_folder_works_outside_cwd(ws, engine):
    # rg мери `--glob` с `/` спрямо cwd, а тестът (и Genesis с друг
    # workspace) търси в папка, която не е cwd: беше „няма съвпадения“.
    assert os.getcwd() != str(ws)
    (ws / "src" / "pages").mkdir(parents=True)
    (ws / "src" / "pages" / "a.tsx").write_text("useRouter()\n", encoding="utf-8")
    (ws / "b.tsx").write_text("useRouter()\n", encoding="utf-8")
    assert _names(repo_map.search_code("useRouter", ws, glob="pages/*.tsx")) == ["a.tsx"]
    assert _names(repo_map.search_code("useRouter", ws, glob="src/**/*.tsx")) == ["a.tsx"]


# ── кодирането след първите 32 KB ────────────────────────────────────────────

def test_read_range_past_the_head_uses_the_whole_file_encoding(ws):
    p = ws / "late.txt"
    p.write_bytes(b"x = 1\n" * 8000 + "# Грешка при запис\n".encode("cp1251"))
    out = gs._tool_read_file("late.txt", offset=8001, limit=1)
    assert "Грешка при запис" in out, out
    assert "cp1251" in out.splitlines()[0]
    hit = repo_map.search_code("Грешка", ws)[0]
    assert hit.line == 8001 and hit.text in out
