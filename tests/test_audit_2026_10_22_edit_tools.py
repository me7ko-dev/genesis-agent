"""Одит на файловите инструменти 2026-10-22 (READ_FILE/WRITE_FILE/EDIT_FILE).
Всеки тест пада без поправката си (пуснато срещу стария код), освен пазача
test_separate_blocks_and_tags_after_them_still_run — той минава и на него."""
from __future__ import annotations

import os
import re
import sys
import threading

import pytest

import genesis_skills as gs
from genesis_agent import code_validate

SEP = gs._EDIT_SEPARATOR


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    gs.set_workspace(tmp_path)
    gs._SEEN_PATHS.clear()
    monkeypatch.setattr("genesis_agent.sandbox._POLICY", gs.sandbox.SandboxPolicy())
    yield tmp_path
    gs.set_workspace(gs._PROJECT_ROOT)


@pytest.fixture
def ran(monkeypatch):
    """Командите, които парсерът би пуснал — записват се, не се пускат (и на Windows)."""
    calls: list[str] = []
    monkeypatch.setitem(gs._SIMPLE_DISPATCH, "RUN_CMD", lambda arg: calls.append(arg) or "ok")
    return calls


def _body(out: str) -> str:
    return out.split("\n", 1)[1]


# ── 1. Текстовите тагове: тяло, което споменава синтаксиса ──────────────────

README = ("## Как Genesis пише файлове\n`[WRITE_FILE: път]съдържание[END_WRITE]`\n"
          "Пример за команда: [RUN_CMD: touch pwned.txt]\n")
TAGS_DOC = ("# Тагове\nЗапис: `[WRITE_FILE: път]съдържание[END_WRITE]`\n"
            f"Редакция: `[EDIT_FILE: път]стар{SEP}нов[END_EDIT]`\nЧетене: `[READ_FILE: път]`\n")
LONE = "Затвори с [END_WRITE] на нов ред.\nПример: [RUN_CMD: touch pwned.txt]\n"
EXAMPLE = ("## WRITE_FILE\nПише файл. Блокът свършва с [END_WRITE].\nПример:\n"
           "[WRITE_FILE: hello.txt]\nздравей\n[END_WRITE]\n")


@pytest.mark.parametrize("reply, before, expected", [
    # README, който показва WRITE_FILE и RUN_CMD — пише се цял, нищо не тръгва.
    (f"[WRITE_FILE: README.md]\n{README}[END_WRITE]", None, ("README.md", README)),
    (f"[WRITE_FILE: tags.md]\n{TAGS_DOC}[END_WRITE]", None, ("tags.md", TAGS_DOC)),
    # Самотно [END_WRITE] в текста, а след него таг: истинският край е на свой ред.
    (f"[WRITE_FILE: c.md]\n{LONE}[END_WRITE]", None, ("c.md", LONE)),
    # Споменат край посред изречение, после цял пример на отделни редове.
    (f"[WRITE_FILE: ex.md]\n{EXAMPLE}[END_WRITE]\n", None, ("ex.md", EXAMPLE)),
    # Разделителят вътре в стария текст: истинският е на свой ред.
    (f"[EDIT_FILE: notes.md]\nРазделител: `{SEP}`\n{SEP}\nРазделител: `{SEP}` (в EDIT_FILE)\n[END_EDIT]",
     ("notes.md", f"Разделител: `{SEP}`\nкрай\n"),
     ("notes.md", f"Разделител: `{SEP}` (в EDIT_FILE)\nкрай\n")),
    # [END_EDIT] вътре в новия текст.
    (f"[EDIT_FILE: g.md]\nold line\n{SEP}\nЗатвори с [END_EDIT] на нов ред.\n[END_EDIT]",
     ("g.md", "old line\n"), ("g.md", "Затвори с [END_EDIT] на нов ред.\n")),
    # Не може да се реши: два затварящи тага на свой ред и команда между тях —
    # отказ, файлът не е пипнат и командата не тръгва.
    ("[WRITE_FILE: amb.md]\nТекст\n[END_WRITE]\n[RUN_CMD: touch pwned.txt]\n[END_WRITE]",
     None, ("amb.md", None)),
    # Два разделителя, нито един сам на ред — отказ, файлът е непроменен.
    (f"[EDIT_FILE: s.md]a{SEP}b{SEP}c[END_EDIT]", ("s.md", f"a{SEP}b\n"), ("s.md", f"a{SEP}b\n")),
], ids=["readme", "tags-doc", "lone-closer", "mention-then-example", "sep-in-old", "end-in-new",
        "ambiguous", "two-inline-seps"])
def test_text_tag_body_that_mentions_the_tags_is_not_cut_and_runs_nothing(
        ws, ran, reply, before, expected) -> None:
    if before:
        (ws / before[0]).write_text(before[1], encoding="utf-8")
    results = gs.parse_and_execute_tools(reply)
    name, content = expected
    assert ran == [], results
    assert len(results) == 1, results
    if content is None:
        assert "❌" in results[0], results
        assert not (ws / name).exists()
    else:
        if before and before[1] == content:
            assert "❌" in results[0], results
        else:
            assert "✓" in results[0], results
        assert (ws / name).read_text(encoding="utf-8") == content


def test_separate_blocks_and_tags_after_them_still_run(ws, ran) -> None:
    """Пазач (минава и на стария код): обичайният отговор не се променя."""
    (ws / "b.py").write_text("x = 1\n", encoding="utf-8")
    reply = ("[WRITE_FILE: a.txt]\nA\n[END_WRITE]\n"
             f"[EDIT_FILE: b.py]\nx = 1\n{SEP}\nx = 2\n[END_EDIT]\n"
             "[WRITE_FILE: c.txt]C[END_WRITE] [RUN_CMD: echo ok]")
    results = gs.parse_and_execute_tools(reply)
    assert len(results) == 4, results
    assert (ws / "a.txt").read_text(encoding="utf-8") == "A\n"
    assert (ws / "b.py").read_text(encoding="utf-8") == "x = 2\n"
    assert (ws / "c.txt").read_text(encoding="utf-8") == "C"
    assert ran == ["echo ok"]


# ── 2. WRITE_FILE след отрязан READ_FILE ────────────────────────────────────

def test_write_after_a_partial_read_is_refused(ws) -> None:
    p = ws / "settings.py"
    p.write_text("".join(f"SETTING_{i} = {'x' * 20!r}\n" for i in range(1, 251)), encoding="utf-8")
    before = p.read_bytes()
    shown = _body(gs.dispatch_tool_call("READ_FILE", {"path": "settings.py"}))
    assert "отрязано" in shown
    shown = shown.rsplit("\n… [отрязано", 1)[0]
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "settings.py",
                                               "content": shown.replace("SETTING_1 ", "SETTING_ONE ")})
    assert "❌" in out, out
    assert p.read_bytes() == before

    # Диапазон от 5 реда — също само част.
    b = ws / "b.txt"
    b.write_text("".join(f"ред {i}\n" for i in range(1, 101)), encoding="utf-8")
    gs.dispatch_tool_call("READ_FILE", {"path": "b.txt", "offset": 1, "limit": 5})
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "b.txt", "content": "x\n"})
    assert "❌" in out, out
    assert b.read_text(encoding="utf-8").count("\n") == 100

    # Прочетено докрай на две части — вече може.
    gs.dispatch_tool_call("READ_FILE", {"path": "b.txt", "offset": 6, "limit": 95})
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "b.txt", "content": "x\n"})
    assert "✓" in out, out

    # Останалото, прочетено от EXPLORE или под-агент, не е видяно от главния.
    from genesis_agent import agents, explore
    agents_dir = ws / ".genesis" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "reader.md").write_text(
        "---\nname: reader\ndescription: Чете.\ntools: READ_FILE\n---\nТи си четец.\n",
        encoding="utf-8")

    def reads_the_middle(messages, tools=None):
        gs.dispatch_tool_call("READ_FILE", {"path": name, "offset": 51, "limit": 30})
        raise RuntimeError("край")

    for name, run in (("c.txt", lambda: explore.explore("?", str(ws), complete=reads_the_middle)),
                      ("d.txt", lambda: agents.run("reader", "x", ws, complete=reads_the_middle))):
        (ws / name).write_text("".join(f"ред {i}\n" for i in range(1, 101)), encoding="utf-8")
        gs.dispatch_tool_call("READ_FILE", {"path": name, "offset": 1, "limit": 50})
        with pytest.raises(RuntimeError):
            run()
        gs.dispatch_tool_call("READ_FILE", {"path": name, "offset": 81, "limit": 20})
        out = gs.dispatch_tool_call("WRITE_FILE", {"path": name, "content": "x\n"})
        assert "❌" in out, (name, out)


# ── 3. WRITE_FILE върху файл, променен след четенето ────────────────────────

def test_write_over_a_file_changed_since_the_read_is_refused(ws) -> None:
    s = ws / "s.txt"
    s.write_text("v1\n", encoding="utf-8")
    gs.dispatch_tool_call("READ_FILE", {"path": "s.txt"})
    s.write_text("v1\nuser added important line\n", encoding="utf-8")
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "s.txt", "content": "v1-model\n"})
    assert "❌" in out, out
    assert s.read_text(encoding="utf-8") == "v1\nuser added important line\n"
    # След ново четене — може; и собственият запис/редакция не го правят „чужд“.
    gs.dispatch_tool_call("READ_FILE", {"path": "s.txt"})
    assert "✓" in gs.dispatch_tool_call("WRITE_FILE", {"path": "s.txt", "content": "v2\n"})
    assert "✓" in gs.dispatch_tool_call("EDIT_FILE", {"path": "s.txt", "old": "v2", "new": "v3"})
    assert "✓" in gs.dispatch_tool_call("WRITE_FILE", {"path": "s.txt", "content": "v4\n"})
    assert s.read_text(encoding="utf-8") == "v4\n"


# ── 4. FIFO при EDIT_FILE и WRITE_FILE ──────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="FIFO е POSIX")
@pytest.mark.parametrize("tool, args, unblock", [
    ("EDIT_FILE", {"path": "pipe", "old": "a", "new": "b"}, "wb"),
    ("WRITE_FILE", {"path": "pipe", "content": "x\n"}, "rb"),
])
def test_edit_and_write_refuse_a_fifo_at_once(ws, tool, args, unblock) -> None:
    os.mkfifo(ws / "pipe")
    result: list[str] = []
    t = threading.Thread(target=lambda: result.append(gs.dispatch_tool_call(tool, args)),
                         daemon=True)
    t.start()
    t.join(5)
    if not result:
        # Отблокираме висящия open(), за да не остане нишката жива.
        with open(ws / "pipe", unblock):
            pass
        t.join(5)
        pytest.fail(f"{tool} виси на FIFO")
    assert "не е обикновен файл" in result[0], result[0]


# ── 5. READ_FILE с offset/limit: изгубени редове ────────────────────────────

def test_read_range_never_drops_lines_silently(ws) -> None:
    (ws / "r.txt").write_text("A" * 7994 + "\nline2\nline3\nline4\n", encoding="utf-8")
    out = gs.dispatch_tool_call("READ_FILE", {"path": "r.txt", "offset": 1, "limit": 4})
    assert "line2" in out and "line4" in out, out[-300:]
    assert "(редове 1-4 от 4)" in out.splitlines()[0]

    (ws / "big.txt").write_text("".join(f"row {i} {'x' * 90}\n" for i in range(1, 2001)),
                                encoding="utf-8")
    out = gs.dispatch_tool_call("READ_FILE", {"path": "big.txt", "offset": 100, "limit": 200})
    m = re.search(r"\(редове 100-(\d+) от 2000\)", out.splitlines()[0])
    assert m, out.splitlines()[0]
    last = int(m.group(1))
    assert last < 299
    shown = [ln for ln in out.splitlines() if re.match(r"\s*\d+\t", ln)]
    assert int(shown[-1].split("\t")[0]) == last
    assert f"offset {last + 1}" in out


# ── 6. WRITE_FILE на .py: ruff не сменя CRLF с LF ───────────────────────────

@pytest.mark.skipif(not code_validate._ruff_available(), reason="няма ruff")
def test_py_write_keeps_crlf_through_ruff(ws) -> None:
    assert code_validate.validate_code_with_ruff("x = 1\r\ny = 2\r\n") == (True, "x = 1\r\ny = 2\r\n")
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "n.py", "content": "x = 1\r\ny = 2\r\n"})
    assert "✓" in out, out
    assert (ws / "n.py").read_bytes() == b"x = 1\r\ny = 2\r\n"
    e = ws / "e.py"
    e.write_bytes(b"a = 1\nb = 2\n")
    gs.dispatch_tool_call("READ_FILE", {"path": "e.py"})
    out = gs.dispatch_tool_call("WRITE_FILE", {"path": "e.py", "content": "a = 1\r\nb = 2\r\n",
                                               "exact": True})
    assert "✓" in out, out
    assert e.read_bytes() == b"a = 1\r\nb = 2\r\n"


# ── 7. Диффът на EDIT_FILE за файл без нов ред в края ───────────────────────

def test_edit_diff_of_a_file_without_trailing_newline_is_readable(ws) -> None:
    (ws / "n.txt").write_bytes(b"one\ntwo")
    out = gs.dispatch_tool_call("EDIT_FILE", {"path": "n.txt", "old": "two", "new": "three"})
    assert "+1/-1 реда" in out, out
    assert ("-two\n\\ No newline at end of file\n+three\n\\ No newline at end of file" in out), out
    assert (ws / "n.txt").read_bytes() == b"one\nthree"


# ── 8. Броячите: замени при replace_all, редове в бележката на READ_FILE ────

def test_counts_shown_to_the_model_are_exact(ws) -> None:
    (ws / "o.txt").write_text("aaa\n", encoding="utf-8")
    out = gs.dispatch_tool_call("EDIT_FILE", {"path": "o.txt", "old": "aa", "new": "b",
                                              "replace_all": True})
    assert (ws / "o.txt").read_text(encoding="utf-8") == "ba\n"
    assert "1 замяна(и)" in out, out
    (ws / "f.txt").write_text("".join(f"line {i}\n" for i in range(1, 2001)), encoding="utf-8")
    out = gs.dispatch_tool_call("READ_FILE", {"path": "f.txt"})
    assert "2000 реда" in out, out[-200:]
