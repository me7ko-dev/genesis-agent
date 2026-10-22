"""Одит на инструментите за търсене (SEARCH_CODE/GLOB/REPO_MAP), 2026-10-22.

По един тест на находка. Всеки падаше на кода преди поправката; тестовете с
`engine` вървят и с ripgrep, и с Python пътя, защото кой двигател ще отговори
зависи от инсталацията, не от заявката (виж test_repo_map_invariants.py).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import genesis_skills as gs
from genesis_agent import repo_map


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"   # tmp_path носи и genesis_data от conftest
    root.mkdir()
    gs.set_workspace(root)
    gs._SEEN_PATHS.clear()
    monkeypatch.setattr("genesis_agent.sandbox._POLICY", gs.sandbox.SandboxPolicy())
    yield root
    gs.set_workspace(gs._PROJECT_ROOT)


@pytest.fixture(params=["rg", "python"])
def engine(request, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(repo_map.shutil, "which", lambda _name: None)
    elif not repo_map.shutil.which("rg"):
        pytest.skip("няма rg")
    return request.param


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _rel(hits, root: Path) -> list[str]:
    return [Path(h.path).resolve().relative_to(root.resolve()).as_posix() for h in hits]


# ── 1. GLOB с `..` изброяваше имената на ключовете извън workspace-а ─────────

def test_glob_with_dotdot_does_not_list_key_names_outside_the_workspace(tmp_path):
    home = tmp_path / "home"
    _write(home, {".ssh/id_ed25519_work": "-----BEGIN OPENSSH PRIVATE KEY-----\n",
                  ".aws/credentials": "[default]\n",
                  "other/b.py": "x = 2\n",
                  "proj/src/a.py": "x = 1\n"})
    gs.set_workspace(home / "proj")
    outs = [gs._tool_glob("../.ssh/*"),
            gs._tool_glob("**/../../.ssh/*"),
            gs._tool_glob(".ssh/* | .."),
            gs._tool_glob(f"{home.as_posix()}/.ssh/*"),
            gs.dispatch_tool_call("GLOB", {"pattern": "../.aws/*"}),
            str(gs.parse_and_execute_tools("[GLOB: ../.ssh/*]"))]
    for out in outs:
        assert "id_ed25519_work" not in out and "credentials" not in out, out
    # Същият отказ като `LIST_DIR ~/.ssh`, не „няма файлове“.
    assert "SANDBOX" in gs._tool_glob("../.ssh/*")
    # Съседна обикновена папка си остава достъпна.
    assert "b.py" in gs._tool_glob("../other/*.py")


# ── 2. build/env/dist… на всяка дълбочина изчезваха, а „низът наистина го няма“ ─

def test_nested_source_folders_named_like_build_output_are_searched(ws, engine):
    _write(ws, {"src/env/schema.mjs": "export const s = {DATABASE_URL: 1}\n",
                "packages/build/tool.py": "def build_helper(): pass\n",
                ".vscode/settings.json": '{"editor.tabSize": 2}\n',
                # истинските артефакти остават извън търсенето
                "build/lib/copy.py": "ARTIFACT_ONLY = 1\n",
                "backend/env/pyvenv.cfg": "home = /usr/bin\n",
                "backend/env/bin/activate": "VENV_ONLY=1\n",
                "node_modules/react/package.json": '{"version": "18.3.1"}\n'})
    assert "schema.mjs" in gs._tool_search_code("DATABASE_URL")
    assert "tool.py" in gs._tool_search_code("build_helper")
    assert "settings.json" in gs._tool_search_code("tabSize")
    assert "schema.mjs" in gs._tool_glob("**/*.mjs")
    assert "settings.json" in gs._tool_glob(".vscode/*.json")
    assert "tool.py" in gs._tool_glob("**/build/*.py")
    assert "packages/  (1 файла)" in gs._tool_repo_map("")
    for needle in ("ARTIFACT_ONLY", "VENV_ONLY"):
        assert "Няма съвпадения" in gs._tool_search_code(needle), needle
    # Празният отговор казва къде НЕ е търсено, вместо „наистина го няма“.
    out = gs._tool_search_code("18.3.1")
    assert "node_modules/" in out and "build/" in out, out
    assert "node_modules/" in gs._tool_glob("**/package.json")


# ── 3. Таванът (60/200) мълчеше, а под rg даваше случайна извадка ─────────────

def test_capped_results_say_so_are_stable_and_keep_the_newest_file(ws, engine):
    for i in range(80):
        _write(ws, {f"pkg{i:02d}/mod.py": "TODO = 1\n"})
    _write(ws, {"pkg05.py": "TODO = 2\n"})
    out = gs._tool_search_code("TODO")
    assert "показани първите 60" in out, out.splitlines()[0]
    hits = repo_map.search_code("TODO", ws)
    # Един и същ отговор при всяко извикване и от двата двигателя.
    assert _rel(hits, ws) == _rel(repo_map._search_python(ws, "TODO", None, 60), ws)
    assert _rel(repo_map.search_code("TODO", ws), ws) == _rel(hits, ws)

    for i in range(230):
        f = ws / "docs" / f"page{i:03d}.md"
        _write(ws, {f"docs/page{i:03d}.md": "x\n"})
        os.utime(f, (1e9, 1e9))
    _write(ws, {"src/app.py": "print('току-що редактиран')\n"})
    out = gs._tool_glob("**/*.md | docs") + gs._tool_glob("**/*")
    assert "src/app.py" in out.replace("\\", "/")
    assert "показани 200 от" in out, out.splitlines()[0]


# ── 4. Шаблон с {a,b} ─────────────────────────────────────────────────────────

def test_brace_patterns_match_in_glob_and_in_both_search_engines(ws, engine):
    _write(ws, {"src/a.ts": "export const foo = 1;\n",
                "src/b.tsx": "export const foo = 2;\n",
                "src/c.js": "foo\n"})
    for pattern in ("**/*.{ts,tsx}", "src/*.{ts,tsx}"):
        out = gs._tool_glob(pattern)
        assert "a.ts" in out and "b.tsx" in out and "c.js" not in out, out
    out = gs._tool_glob("src/*.{ts,{tsx,js}}")
    assert "a.ts" in out and "b.tsx" in out and "c.js" in out, out
    out = gs._tool_search_code("foo", "", "*.{ts,tsx}")
    assert "a.ts" in out and "b.tsx" in out and "c.js" not in out, out
    out = gs._tool_search_code("foo", "src/*.{ts,tsx}")
    assert "a.ts" in out and "b.tsx" in out and "c.js" not in out, out


# ── 5. GLOB с абсолютен шаблон: вътрешна грешка на английски ──────────────────

def test_glob_with_an_absolute_pattern_finds_the_files(ws):
    _write(ws, {"src/a.py": "x = 1\n", "src/sub/b.py": "x = 2\n", "top.txt": "t\n"})
    outs = [gs._tool_glob(f"{ws}/**/*.py"),
            gs._tool_glob(f"{ws.as_posix()}/src/**/*.py"),
            gs.dispatch_tool_call("GLOB", {"pattern": f"{ws}/**/*.py"}),
            str(gs.parse_and_execute_tools(f"[GLOB: {ws}/**/*.py]"))]
    for out in outs:
        assert "Non-relative" not in out and "Грешка" not in out, out
        assert "a.py" in out and "b.py" in out and "top.txt" not in out, out
    out = gs._tool_glob(f"{ws}/src/*.py")
    assert "a.py" in out and "b.py" not in out, out


# ── 6. Дълъг ред: съвпадението оставаше отвъд първите 200 знака ──────────────

def test_a_long_matched_line_shows_the_match(ws, engine):
    (ws / "dist.min.js").write_text("var a=1;" * 400 + "function secretHandler(){}"
                                    + "var b=2;" * 100 + "\n", encoding="utf-8")
    (ws / "lat.txt").write_bytes(b"caf\xe9 " * 100 + b"ASCIINEEDLE" + b" caf\xe9" * 50 + b"\n")
    for needle, name in (("secretHandler", "dist.min.js"), ("ASCIINEEDLE", "lat.txt")):
        hits = repo_map.search_code(needle, ws)
        assert len(hits) == 1 and hits[0].path.endswith(name), hits
        text = hits[0].text
        assert needle in text and len(text) <= repo_map._LINE_CLIP, text
        assert text.startswith("…") and text.endswith("…"), text
        assert needle in gs._tool_search_code(needle)


# ── 7. Python пътят без rg: POSIX класове не съвпадаха с нищо ────────────────

def test_posix_classes_and_unicode_classes_without_ripgrep(ws, engine):
    _write(ws, {"src/a.py": "def Foo(x):\n    return 42\nname = 'Ünïcode'\n"})
    assert [h.line for h in repo_map.search_code("[[:upper:]]oo", ws)] == [1]
    assert [h.line for h in repo_map.search_code("return [[:digit:]]+$", ws)] == [2]
    assert [h.line for h in repo_map.search_code("x[^[:alpha:]]", ws)] == [1]
    out = gs._tool_search_code(r"\p{Lu}nïcode")
    assert "наистина го няма" not in out, out
    if engine == "python":
        assert "ripgrep" in out and r"\p{" in out, out
    else:
        assert "a.py:3" in out, out


# ── 8. REPO_MAP: .mjs/.cjs/.mts/.astro не се броят ───────────────────────────

def test_repo_map_counts_modern_js_suffixes(ws):
    _write(ws, {f"src/{n}": "export default 1\n"
                for n in ("index.mjs", "server.mjs", "db.cjs", "types.mts", "page.astro")})
    _write(ws, {"package.json": '{"type":"module","scripts":{"test":"node --test"}}\n'})
    out = gs._tool_repo_map("")
    assert "src/  (5 файла)" in out, out
    assert ".mjs×2" in out, out
