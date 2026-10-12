"""WRITE_FILE на .py с буквални `\\n` вместо нови редове (2026-10-12).

bench_all на f8f08d7: 8 .py файла в 4 проекта бяха записани така (faktura-excel
0/1, файлът по-долу е съкратен от faktura-excel-1/faktura/extract.py). Записът
минаваше с „✓“, защото ruff го няма в PATH на лаптопа. Тук ruff е изключен
нарочно — проверката трябва да работи и без него.
"""
from __future__ import annotations

import ast

import pytest

import genesis_skills as gs

# Както го написа моделът: истински нови редове в документацията, буквални
# „\n“ след нея; регексите (`\d`) са правилни и трябва да останат.
BROKEN = r"""'''extract.py
Extract invoice data from a PDF.
'''\n
from __future__ import annotations
\nimport re
import datetime\n\n# Regular expression components\n_NUMBER = r"\d{1,10}"\nDATE = r"\d{4}-\d{2}-\d{2}"\n\n\ndef _line(text):\n    return re.findall(r"[^\n]+", text)\n\n\ndef extract(text):\n    return {"nomer": re.findall(_NUMBER, text), "data": datetime.date.today()}\n"""


@pytest.fixture(autouse=True)
def _workspace(tmp_path, monkeypatch):
    gs.set_workspace(tmp_path)
    monkeypatch.setattr("genesis_agent.sandbox._POLICY", gs.sandbox.SandboxPolicy())
    monkeypatch.setattr("genesis_agent.code_validate._ruff_available", lambda: False)
    yield tmp_path
    gs.set_workspace(gs._PROJECT_ROOT)


def test_literal_newlines_in_python_are_repaired(_workspace) -> None:
    out = gs._tool_write_file("faktura/extract.py", BROKEN)
    text = (_workspace / "faktura/extract.py").read_text(encoding="utf-8")
    ast.parse(text)                                   # вече е Python
    assert r'_NUMBER = r"\d{1,10}"' in text           # регексите не са пипнати
    assert r'r"[^\n]+"' in text                        # „\n“ в низ е код, не нов ред
    assert "# Regular expression components\n_NUMBER" in text
    assert "✓" in out and "поправени" in out


def test_valid_python_with_a_newline_escape_in_a_string_is_untouched(_workspace) -> None:
    code = 'SEP = "\\n"\nprint(SEP.join(["a", "b"]), "x\\ny", "\\n")\n'
    out = gs._tool_write_file("ok.py", code)
    assert (_workspace / "ok.py").read_text(encoding="utf-8") == code
    assert "⚠" not in out


def test_a_newline_escape_inside_a_string_survives_the_repair(_workspace) -> None:
    code = 'import os\\nimport re\\n\\nprint("\\n".join(["a"]))\\n'
    gs._tool_write_file("mixed.py", code)
    text = (_workspace / "mixed.py").read_text(encoding="utf-8")
    assert text == 'import os\nimport re\n\nprint("\\n".join(["a"]))\n'


def test_a_repair_that_does_not_parse_is_not_written(_workspace) -> None:
    """Истинска грешка освен изгубените редове: файлът остава какъвто е
    написан, а моделът научава реда със грешката."""
    code = 'import os\\nimport re\\n\\nprint(1 +)\\n'
    out = gs._tool_write_file("bad_too.py", code)
    assert (_workspace / "bad_too.py").read_text(encoding="utf-8") == code
    assert "не е валиден Python" in out and "ред 1" in out


def test_broken_python_without_literal_newlines_says_where(_workspace) -> None:
    out = gs._tool_write_file("bad.py", "def f(:\n    pass\n")
    assert "✓" in out and "не е валиден Python" in out and "ред 1" in out


def test_other_files_keep_literal_newlines(_workspace) -> None:
    text = "ред 1\\nред 2\\nред 3\\nред 4"
    out = gs._tool_write_file("notes.txt", text)
    assert (_workspace / "notes.txt").read_text(encoding="utf-8") == text
    assert "⚠" not in out
