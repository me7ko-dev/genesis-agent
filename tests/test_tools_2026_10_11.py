"""Одит на инструментите 2026-10-11: кодировки (cp1251, BOM, CRLF, UTF-16),
големи и двоични файлове, имена с `[ ]`. Всеки тест пада без поправката си
(пуснато срещу стария код) — освен двата пазача, които минават и на него:
test_search_engines_see_the_same_files[rg] и
test_write_over_mixed_file_and_new_file_use_lf (ревю 2026-10-11)."""
from __future__ import annotations

import os
import sys
import threading
import tracemalloc

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
    assert "\ufffd" not in out


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
    assert "\ufffd" in body
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


# ── read-unbounded ───────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="FIFO е POSIX")
def test_read_file_refuses_a_fifo_at_once(ws):
    os.mkfifo(ws / "pipe")
    result: list[str] = []
    t = threading.Thread(target=lambda: result.append(gs._tool_read_file("pipe")), daemon=True)
    t.start()
    t.join(5)
    if not result:
        # Отблокираме висящия open(), за да не остане нишката жива.
        with open(ws / "pipe", "wb"):
            pass
        t.join(5)
        pytest.fail("READ_FILE виси на FIFO")
    assert "не е обикновен файл" in result[0]


def test_read_file_on_a_big_file_stays_small_in_memory(ws):
    p = ws / "big.log"
    line = b"2026-10-11 12:00:00 INFO something happened here\n"
    with open(p, "wb") as fh:
        fh.write(line * (50_000_000 // len(line)))
    tracemalloc.start()
    try:
        out = gs._tool_read_file("big.log", offset=10, limit=5)
        _, peak_range = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        head = gs._tool_read_file("big.log")
        _, peak_head = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert "   10\t2026-10-11" in out and "   14\t" in out and "   15\t" not in out
    assert "отрязано" in head
    assert peak_range < 5_000_000, peak_range
    assert peak_head < 5_000_000, peak_head


# ── read-binary ──────────────────────────────────────────────────────────────

def test_read_file_refuses_binary_without_garbage(ws):
    import zlib
    (ws / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00" + zlib.compress(bytes(range(256)) * 40))
    out = gs._tool_read_file("img.png")
    assert "двоичен файл" in out and ".png" in out
    assert "PNG" not in out.split("двоичен", 1)[1]
    assert len(out) < 400


def test_read_file_shows_utf16_text(ws):
    (ws / "w.txt").write_bytes("hello\r\nсвят\r\n".encode("utf-16"))
    out = gs._tool_read_file("w.txt")
    assert "hello\nсвят" in out
    assert "\x00" not in out
    ranged = gs._tool_read_file("w.txt", offset=2, limit=1)
    assert "    2\tсвят" in ranged and "от 2)" in ranged


# ── read-range-linenumbers ───────────────────────────────────────────────────

def test_read_range_numbers_lines_like_search_and_edit(ws):
    (ws / "m.py").write_text("a = '\f'\nb = '\u2028'\nc = 1\nd = 2\nTARGET = 3\n", encoding="utf-8")
    from genesis_agent.repo_map import search_code
    [hit] = search_code("TARGET", ws)
    out = gs._tool_read_file("m.py", offset=hit.line, limit=1)
    assert f"{hit.line:>5}\tTARGET = 3" in out
    assert "от 5)" in out


def test_read_range_offset_past_eof_says_so(ws):
    (ws / "a.py").write_text("".join(f"x{i} = {i}\n" for i in range(300)), encoding="utf-8")
    out = gs._tool_read_file("a.py", offset=500, limit=10)
    assert "300 реда" in out and "след" in out


# ── bracket-filenames ────────────────────────────────────────────────────────

@pytest.mark.parametrize("engine", ["rg", "python"])
def test_bracket_filenames_are_found_by_search_and_glob(ws, monkeypatch, engine):
    from genesis_agent import repo_map
    if engine == "python":
        monkeypatch.setattr(repo_map.shutil, "which", lambda _name: None)
    elif not repo_map.shutil.which("rg"):
        pytest.skip("няма rg")
    (ws / "pages" / "[id]").mkdir(parents=True)
    (ws / "pages" / "[slug].tsx").write_text("const r = useRouter()\n", encoding="utf-8")
    (ws / "pages" / "[id]" / "page.tsx").write_text("useRouter()\n", encoding="utf-8")
    out = gs.dispatch_tool_call("SEARCH_CODE", {"pattern": "useRouter", "path": "pages/[slug].tsx"})
    assert "1 съвпадения" in out, out
    hits = repo_map.search_code("useRouter", ws / "pages" / "[id]" / "page.tsx")
    assert len(hits) == 1
    assert "[slug].tsx" in gs._tool_glob("pages/[slug].tsx")
    assert "page.tsx" in gs._tool_glob("pages/[id]/page.tsx")
    # Истинският клас знаци продължава да работи.
    assert "[slug].tsx" in gs._tool_glob("pages/[[]slug].tsx")


# ── search-python-suffixes ───────────────────────────────────────────────────

_NO_SUFFIX_OR_RARE = ["Dockerfile", "Makefile", "run.ps1", "run.bat", ".env.example",
                      "pom.xml", "mod.mts", "mod.cjs"]


@pytest.mark.parametrize("engine", ["rg", "python"])
def test_search_engines_see_the_same_files(ws, monkeypatch, engine):
    from genesis_agent import repo_map
    if engine == "python":
        monkeypatch.setattr(repo_map.shutil, "which", lambda _name: None)
    elif not repo_map.shutil.which("rg"):
        pytest.skip("няма rg")
    for name in _NO_SUFFIX_OR_RARE:
        (ws / name).write_text("API_PORT = 8080\n", encoding="utf-8")
    (ws / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"API_PORT\n")
    hits = repo_map.search_code("API_PORT", ws)
    found = sorted(os.path.basename(h.path) for h in hits)
    assert found == sorted(_NO_SUFFIX_OR_RARE)
    # Изрично подаден файл без разширение също се търси.
    assert len(repo_map.search_code("API_PORT", ws / "Dockerfile")) == 1


# ── export-readme-fence ──────────────────────────────────────────────────────

def test_readme_comment_inside_a_code_fence_is_not_a_heading(ws):
    from genesis_agent import deliver
    (ws / "README.md").write_text(
        "# tool\n\n## How to run\n\n```bash\n# install deps\npip install -r requirements.txt\n"
        "# start the server\npython main.py\n```\n\n## License\n\nMIT\n", encoding="utf-8")
    sections = deliver.readme_sections(ws)
    assert set(sections) == {"tool", "how to run", "license"}
    body = sections["how to run"]
    assert "pip install -r requirements.txt" in body and "python main.py" in body
    assert body.count("```") == 2


# ── deps-utf16-bom ───────────────────────────────────────────────────────────

_APP = "import flask\nimport requests\n"


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8-sig"])
def test_requirements_with_a_bom_are_read(ws, encoding):
    from genesis_agent import deliver, py_deps
    (ws / "app.py").write_text(_APP, encoding="utf-8")
    (ws / "requirements.txt").write_bytes("requests==2.32.3\r\nflask==3.0.0\r\n".encode(encoding))
    assert py_deps.declared(ws) == {"requests", "flask"}
    assert py_deps.undeclared(ws) == []
    assert deliver._dependencies(ws, ["requirements.txt"]) == ["requests==2.32.3", "flask==3.0.0"]


def test_pyproject_with_a_bom_is_read(ws):
    from genesis_agent import py_deps
    (ws / "app.py").write_text(_APP, encoding="utf-8")
    (ws / "pyproject.toml").write_bytes(
        b'\xef\xbb\xbf[project]\nname = "x"\ndependencies = ["requests", "flask>=3"]\n')
    assert py_deps.declared(ws) == {"requests", "flask"}
    assert py_deps.undeclared(ws) == []


# ── write-crlf-bom ───────────────────────────────────────────────────────────

def _read_write(ws, name: str, old: str, new: str) -> bytes:
    body = _body(gs._tool_read_file(name))
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": name, "content": body.replace(old, new)})
    assert "✓" in out, out
    return (ws / name).read_bytes()


def test_write_over_crlf_bom_file_keeps_endings_and_bom(ws):
    (ws / "run.bat").write_bytes(b"\xef\xbb\xbf@echo off\r\nset A=1\r\necho %A%\r\n")
    assert _read_write(ws, "run.bat", "A=1", "A=2") == b"\xef\xbb\xbf@echo off\r\nset A=2\r\necho %A%\r\n"


def test_write_over_bom_file_without_bom_in_content_adds_it_back(ws):
    (ws / "a.txt").write_bytes(b"\xef\xbb\xbfx=1\n")
    gs._tool_read_file("a.txt")
    gs.dispatch_tool_call("WRITE_FILE", {"path": "a.txt", "content": "x=2\n"})
    assert (ws / "a.txt").read_bytes() == b"\xef\xbb\xbfx=2\n"


def test_write_can_convert_crlf_to_lf_and_drop_a_bom(ws):
    """Ревю 2026-10-11: „обърни run.sh в LF“ връщаше ✓, а файлът оставаше CRLF
    (и BOM-ът се връщаше) — нито WRITE_FILE, нито EDIT_FILE можеха да го сменят."""
    (ws / "run.sh").write_bytes(b"#!/bin/sh\r\necho hi\r\n")
    gs._tool_read_file("run.sh")
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "run.sh", "content": "#!/bin/sh\necho hi\n",
                                               "exact": True})
    assert "✓" in out and (ws / "run.sh").read_bytes() == b"#!/bin/sh\necho hi\n"
    (ws / "package.json").write_bytes(b"\xef\xbb\xbf{}\n")
    gs._tool_read_file("package.json")
    gs.dispatch_tool_call("WRITE_FILE", {"path": "package.json", "content": "{}\n", "exact": "true"})
    assert (ws / "package.json").read_bytes() == b"{}\n"


def test_the_write_preview_shows_a_dropped_bom_only_with_exact(ws):
    from genesis_agent import agent_core
    (ws / "a.txt").write_bytes(b"\xef\xbb\xbfx\n")
    assert agent_core._diff_for_write(gs, {"path": "a.txt", "content": "x\n"}) is None
    assert agent_core._diff_for_write(gs, {"path": "a.txt", "content": "x\n", "exact": True})


def test_write_says_when_it_kept_the_files_crlf_or_bom(ws):
    """Без `exact` стилът на файла остава, но моделът вече го научава от резултата."""
    (ws / "run.sh").write_bytes(b"\xef\xbb\xbf#!/bin/sh\r\necho hi\r\n")
    gs._tool_read_file("run.sh")
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "run.sh", "content": "#!/bin/sh\necho hi\n"})
    assert (ws / "run.sh").read_bytes() == b"\xef\xbb\xbf#!/bin/sh\r\necho hi\r\n"
    assert "CRLF" in out and "BOM" in out and "exact=true" in out
    (ws / "a.txt").write_bytes(b"x\n")
    gs._tool_read_file("a.txt")
    assert "exact" not in gs.dispatch_tool_call("WRITE_FILE", {"path": "a.txt", "content": "y\n"})


def test_write_over_utf16_file_keeps_it_readable(ws):
    (ws / "w.txt").write_bytes("hello\r\nсвят\r\n".encode("utf-16"))
    raw = _read_write(ws, "w.txt", "hello", "здравей")
    assert raw == "здравей\r\nсвят\r\n".encode("utf-16")


def test_write_over_mixed_file_and_new_file_use_lf(ws):
    (ws / "mixed.txt").write_bytes(b"a\r\nb\nc\n")
    assert _read_write(ws, "mixed.txt", "c", "d") == b"a\nb\nd\n"
    gs.dispatch_tool_call("WRITE_FILE", {"path": "new.sh", "content": "echo 1\necho 2\n"})
    assert (ws / "new.sh").read_bytes() == b"echo 1\necho 2\n"
