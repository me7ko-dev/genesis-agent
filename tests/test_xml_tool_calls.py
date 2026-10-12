"""XML формата на Claude за нашите инструменти (2026-10-12).

Формите са взети от 30 сурови отговора на `claude -p` в bench (vat/eik/iban ×2):
6 започваха с `<invoke name="…">`; отговор само с XML свършваше хода без нито
един инструмент (vat-check 0/1). Пътищата тук са съкратени, формата — същата.
"""
from __future__ import annotations

import pytest

import genesis_skills as gs


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(gs, "_WORKSPACE", tmp_path)
    return tmp_path


@pytest.mark.parametrize("opening", [
    '<invoke name="WRITE_FILE">eik.py</parameter>\n',                       # bench, отговор 0
    ('<invoke name="WRITE_FILE">\n<parameter name="path">eik.py</parameter>\n'
     '<parameter name="content">'),                                          # отговор 6
    '<invoke name="WRITE_FILE">\neik.py\n',                                 # отговор 18
    '<invoke name="WRITE_FILE">eik.py</invoke>\n',                          # отговор 24
])
def test_an_xml_opened_write_writes_the_file(ws, opening):
    reply = ("Пиша файла.\n" + opening + 'X = [1, 2]\nprint(X[0])\n</parameter>\n'
             "[END_WRITE]\n[WRITE_FILE: tests/t.py]\nok = True\n[END_WRITE]")
    if "<parameter name=\"content\">" not in opening:
        reply = reply.replace("</parameter>\n[END_WRITE]", "[END_WRITE]", 1)
    gs.parse_and_execute_tools(reply)
    assert (ws / "eik.py").read_text(encoding="utf-8") == "X = [1, 2]\nprint(X[0])\n"
    assert (ws / "tests" / "t.py").exists()


def test_a_reply_with_only_an_xml_command_runs_it(ws):
    out = gs.parse_and_execute_tools('<invoke name="RUN_CMD">python -c "print(6*7)"</invoke>')
    assert len(out) == 1 and "42" in out[0]


def test_xml_closers_inside_a_bracket_tag_do_not_reach_the_shell(ws):
    out = gs.parse_and_execute_tools('[RUN_CMD: python -c "print(5)"</parameter>\n</invoke>]')
    assert len(out) == 1 and "5" in out[0] and "parameter" not in out[0]


def test_the_same_command_as_xml_and_as_a_tag_runs_once(ws):
    reply = ('<invoke name="RUN_CMD">python -c "print(1)"</invoke>\n\n'
             '[RUN_CMD: python -c "print(1)"]')
    assert len(gs.parse_and_execute_tools(reply)) == 1


def test_text_without_xml_is_left_byte_for_byte():
    text = "[WRITE_FILE: a.py]\nx = '<b>'\n[END_WRITE]\n[RUN_CMD: echo '</p>']"
    assert gs._normalize_xml_tool_calls(text) == text


def test_xml_with_an_unknown_tool_name_stays_text():
    text = '<invoke name="Bash">rm -rf /</invoke>'
    assert gs._normalize_xml_tool_calls(text) == text


def test_an_xml_attempt_that_is_not_understood_still_gets_the_syntax_nudge():
    assert gs.looks_like_attempted_tool_tag('<invoke name="Bash">\n<parameter name="command">ls')


def test_the_newline_right_after_the_write_tag_is_not_content(ws):
    """Преди 2026-10-12 всеки файл от `[WRITE_FILE: x]⏎…` започваше с празен ред."""
    gs.parse_and_execute_tools("[WRITE_FILE: run.sh]\n#!/bin/sh\necho hi\n[END_WRITE]")
    assert (ws / "run.sh").read_text(encoding="utf-8").startswith("#!/bin/sh")
    gs.parse_and_execute_tools("[WRITE_FILE: same.txt]inline\n[END_WRITE]")
    assert (ws / "same.txt").read_text(encoding="utf-8") == "inline\n"


# ── пълното XML извикване (bench_fix с claude_cli, 2026-10-12: 0/13 преди) ────

def test_a_full_xml_edit_with_named_parameters_edits_the_file(ws):
    (ws / "stats.py").write_text("def median(s):\n    return s[len(s) // 2]\n", encoding="utf-8")
    reply = ('<invoke name="EDIT_FILE">\n<parameter name="path">stats.py</parameter>\n'
             '<parameter name="old_string">    return s[len(s) // 2]</parameter>\n'
             '<parameter name="new_string">    mid = len(s) // 2\n    return s[mid]</parameter>\n</invoke>')
    out = gs.parse_and_execute_tools(reply)
    assert len(out) == 1 and "✓" in out[0], out
    assert (ws / "stats.py").read_text(encoding="utf-8") == (
        "def median(s):\n    mid = len(s) // 2\n    return s[mid]\n")


def test_a_full_xml_write_and_read(ws):
    gs.parse_and_execute_tools('<invoke name="WRITE_FILE"><parameter name="file_path">a.py</parameter>'
                               '<parameter name="content">x = 1\n</parameter></invoke>')
    assert (ws / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    out = gs.parse_and_execute_tools('<tool_call>READ_FILE</tool_call>\n<arg name="path">a.py</arg>')
    assert len(out) == 1 and "x = 1" in out[0]


def test_a_full_xml_command_runs_and_background_means_run_bg():
    assert gs._normalize_xml_tool_calls(
        '<invoke name="RUN_CMD"><parameter name="command">pytest -q</parameter></invoke>'
    ) == "[RUN_CMD: pytest -q]"
    assert gs._normalize_xml_tool_calls(
        '<invoke name="RUN_CMD"><parameter name="command">npm run dev</parameter>'
        '<parameter name="background">true</parameter></invoke>') == "[RUN_BG: npm run dev]"


def test_an_invented_tool_stays_text_and_gets_the_nudge():
    text = '<invoke name="RUN_TESTS"><parameter name="command">pytest</parameter></invoke>'
    assert gs._normalize_xml_tool_calls(text) == text
    assert gs.looks_like_attempted_tool_tag(text)


def test_a_heredoc_command_closed_with_end_cmd_runs():
    """Claude, 2026-10-12: `[RUN_CMD: python - <<'EOF' … EOF[END_CMD]` губеше рунд."""
    reply = "[RUN_CMD: python - <<'EOF'\nprint(sum([1, 2]))\nEOF[END_CMD]"
    assert gs._SIMPLE_RE.search(gs._normalize_xml_tool_calls(reply)).group("arg").endswith("EOF")
    assert gs._normalize_xml_tool_calls("[END_CMD] сам по себе си") == "[END_CMD] сам по себе си"
