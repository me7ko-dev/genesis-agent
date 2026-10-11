"""Одит на инструментите 2026-10-11: кодировки (cp1251, BOM, CRLF, UTF-16),
големи и двоични файлове, имена с `[ ]`. Всеки тест пада без поправката си
(пуснато срещу стария код)."""
from __future__ import annotations

import pytest

import genesis_skills as gs
from genesis_agent import code_edit


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    gs.set_workspace(tmp_path)
    gs._SEEN_PATHS.clear()
    monkeypatch.setattr("genesis_agent.sandbox._POLICY", gs.sandbox.SandboxPolicy())
    yield tmp_path
    gs.set_workspace(gs._PROJECT_ROOT)


def _body(out: str) -> str:
    return out.split("\n", 1)[1]


# ── enc-cp1251-dataloss ──────────────────────────────────────────────────────

def test_cp1251_read_is_readable_and_names_the_encoding(ws):
    (ws / "legacy.txt").write_bytes("Здравей, свят\nред две\n".encode("cp1251"))
    out = gs._tool_read_file("legacy.txt")
    assert "Здравей, свят" in out
    assert "cp1251" in out.splitlines()[0]
    assert "�" not in out


def test_cp1251_edit_keeps_the_encoding(ws):
    p = ws / "legacy.txt"
    p.write_bytes("Здравей, свят\nname = 1\n".encode("cp1251"))
    out = gs.dispatch_tool_call("EDIT_FILE", {"path": "legacy.txt", "old": "name = 1", "new": "name = 2"})
    assert "✓" in out, out
    assert p.read_bytes() == "Здравей, свят\nname = 2\n".encode("cp1251")


def test_cp1251_read_then_write_roundtrip_keeps_the_bytes(ws):
    p = ws / "legacy.txt"
    p.write_bytes("Здравей, свят\nname = 1\n".encode("cp1251"))
    body = _body(gs._tool_read_file("legacy.txt"))
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "legacy.txt",
                                               "content": body.replace("name = 1", "name = 2")})
    assert "✓" in out, out
    assert p.read_bytes() == "Здравей, свят\nname = 2\n".encode("cp1251")


def test_edit_of_utf8_py_with_one_latin1_byte_keeps_that_byte(ws):
    p = ws / "m.py"
    raw = b"# (c) 2001 \xa9 Acme\nX = 1\n"
    p.write_bytes(raw)
    out = gs.dispatch_tool_call("EDIT_FILE", {"path": "m.py", "old": "X = 1", "new": "X = 2"})
    assert "✓" in out, out
    assert p.read_bytes() == raw.replace(b"X = 1", b"X = 2")
    # Към модела не излизат самотни сурогати — те чупят JSON заявката.
    out.encode("utf-8")


def test_write_over_file_with_stray_bytes_refuses_replacement_chars(ws):
    p = ws / "m.py"
    raw = b"# (c) \xa9 Acme\nX = 1\n"
    p.write_bytes(raw)
    body = _body(gs._tool_read_file("m.py"))
    assert "�" in body
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "m.py", "content": body.replace("X = 1", "X = 2")})
    assert "❌" in out
    assert p.read_bytes() == raw


def test_search_code_finds_cyrillic_in_cp1251_file_on_both_engines(ws, monkeypatch):
    from genesis_agent import repo_map
    (ws / "msg.txt").write_bytes("ok Грешка при запис\n".encode("cp1251"))
    for engine in ("rg", None):
        if engine is None:
            monkeypatch.setattr(repo_map.shutil, "which", lambda _name: None)
        elif not repo_map.shutil.which("rg"):
            continue
        hits = repo_map.search_code("Грешка", ws)
        assert [h.text for h in hits] == ["ok Грешка при запис"], engine
        hits = repo_map.search_code("ok", ws)
        assert [h.text for h in hits] == ["ok Грешка при запис"], engine


def test_decode_helper_round_trips_every_kind():
    samples = [
        "абв\r\n".encode("cp1251"),
        b"\xef\xbb\xbfkey=1\r\n",
        "hello\nсвят\n".encode("utf-16"),
        b"plain\n",
        b"mostly utf8 \xc3\xa9 and one \xa9 stray\n",
        b"\xe7\x98\xc0\xc1\xc2",   # cp1251 с дупката 0x98
    ]
    for raw in samples:
        text, enc = code_edit.decode_text(raw)
        assert code_edit.encode_text(text, enc) == raw, (raw, enc)
