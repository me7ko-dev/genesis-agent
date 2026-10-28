"""`[END_WRITE_FILE]`/`[END_EDIT_FILE]` — краят, който Sonnet 5.5 пише сам
(bench 2026-10-28): приема се като `[END_WRITE]`/`[END_EDIT]`. Записът —
в test_genesis_skills (там е работната папка на теста)."""
from genesis_agent import brain
from genesis_agent.tool_schemas import normalize_block_closers, open_block_tag


def test_normalize():
    assert normalize_block_closers("[WRITE_FILE: a]x[END_WRITE_FILE]") == "[WRITE_FILE: a]x[END_WRITE]"
    assert normalize_block_closers("[EDIT_FILE: a]x[END_EDIT_FILE]") == "[EDIT_FILE: a]x[END_EDIT]"
    assert normalize_block_closers("") == ""


def test_long_closer_is_not_open_block():
    text = "[WRITE_FILE: app.py]\nx = 1\n[END_WRITE_FILE]"
    assert open_block_tag(text) == ""
    assert not brain._has_open_file_tag(text)
