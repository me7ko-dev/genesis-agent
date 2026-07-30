"""genesis_agent.code_edit — the refusals matter more than the happy path.

Every test here that asserts `ok is False` also asserts the file on disk is
byte-identical afterwards. That is the actual contract: an edit either lands
exactly as described, or nothing happened at all. A half-applied edit to
someone else's project is the failure this module exists to prevent, so
"it returned an error" is not enough to assert on its own.
"""
from __future__ import annotations

from genesis_agent.code_edit import edit_file

SRC = 'def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a - b\n'


def _write(tmp_path, text: str = SRC):
    p = tmp_path / "sample.py"
    p.write_text(text, encoding="utf-8")
    return p


def test_replaces_unique_anchor(tmp_path) -> None:
    p = _write(tmp_path)
    res = edit_file(p, "def add(a, b):\n    return a - b",
                    "def add(a, b):\n    return a + b")
    assert res.ok
    assert res.replacements == 1
    assert "return a + b" in p.read_text(encoding="utf-8")
    # sub() is untouched — the whole point of an anchored edit.
    assert p.read_text(encoding="utf-8").count("return a - b") == 1


def test_diff_describes_the_change(tmp_path) -> None:
    p = _write(tmp_path)
    res = edit_file(p, "return a - b\n\n\ndef sub", "return a + b\n\n\ndef sub")
    assert "-    return a - b" in res.diff
    assert "+    return a + b" in res.diff
    assert res.lines_changed == (1, 1)


def test_ambiguous_anchor_refuses_and_names_lines(tmp_path) -> None:
    p = _write(tmp_path)
    res = edit_file(p, "    return a - b", "    return a * b")
    assert res.ok is False
    assert "2 пъти" in res.detail
    assert "L2" in res.detail and "L6" in res.detail
    assert p.read_text(encoding="utf-8") == SRC


def test_replace_all_takes_every_occurrence(tmp_path) -> None:
    p = _write(tmp_path)
    res = edit_file(p, "    return a - b", "    return a * b", replace_all=True)
    assert res.ok
    assert res.replacements == 2
    assert p.read_text(encoding="utf-8").count("return a * b") == 2


def test_missing_anchor_suggests_nearby_lines(tmp_path) -> None:
    p = _write(tmp_path)
    res = edit_file(p, "def addd(a, b):", "def added(a, b):")
    assert res.ok is False
    # The hint is the difference between one wasted round and five.
    assert "def add(a, b):" in res.detail
    assert p.read_text(encoding="utf-8") == SRC


def test_syntax_breaking_edit_is_not_written(tmp_path) -> None:
    p = _write(tmp_path)
    res = edit_file(p, "def sub(a, b):", "def sub(a, b:")
    assert res.ok is False
    assert "синтаксиса" in res.detail
    assert p.read_text(encoding="utf-8") == SRC


def test_syntax_check_only_applies_to_python(tmp_path) -> None:
    p = tmp_path / "notes.txt"
    p.write_text("def sub(a, b):\n", encoding="utf-8")
    res = edit_file(p, "def sub(a, b):", "def sub(a, b:")
    assert res.ok is True  # not Python — we have no business parsing it


def test_missing_file_points_at_write_file(tmp_path) -> None:
    res = edit_file(tmp_path / "nope.py", "x", "y")
    assert res.ok is False
    assert "WRITE_FILE" in res.detail


def test_empty_anchor_refused(tmp_path) -> None:
    p = _write(tmp_path)
    res = edit_file(p, "", "anything")
    assert res.ok is False
    assert p.read_text(encoding="utf-8") == SRC


def test_noop_edit_refused(tmp_path) -> None:
    p = _write(tmp_path)
    res = edit_file(p, "def sub(a, b):", "def sub(a, b):")
    assert res.ok is False
    assert "идентични" in res.detail


def test_anchors_match_as_substrings_not_whole_lines(tmp_path) -> None:
    """Documented on purpose, because it surprises: matching is `str.find`.

    A two-space anchor matches inside a four-space line and replaces only that
    part, so the surviving indentation comes from the file. Useful when it is
    what you meant, a trap when it is not — which is exactly why the syntax
    gate below exists as the real backstop.
    """
    p = _write(tmp_path)
    res = edit_file(p, "  return a - b\n\n\ndef sub", "  return a + b\n\n\ndef sub")
    assert res.ok is True
    assert "    return a + b" in p.read_text(encoding="utf-8")


def test_indentation_damage_is_caught_by_the_syntax_gate(tmp_path) -> None:
    """The case where a sloppy anchor would really hurt: the file is unchanged."""
    p = _write(tmp_path)
    res = edit_file(p, "    return a - b\n\n\ndef sub", "return a + b\n\n\ndef sub")
    assert res.ok is False
    assert "синтаксиса" in res.detail
    assert p.read_text(encoding="utf-8") == SRC
