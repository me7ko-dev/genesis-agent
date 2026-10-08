"""genesis_agent.py_deps — third-party imports vs what the project declares."""
from __future__ import annotations

from pathlib import Path

import pytest

from genesis_agent import py_deps


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_third_party_imports_skip_stdlib_local_and_relative(tmp_path) -> None:
    _write(tmp_path, "app.py", "import os, json\nimport requests\nfrom bs4 import BeautifulSoup\n"
                               "from helpers import x\nfrom . import y\nimport mypkg.sub\n")
    _write(tmp_path, "helpers.py", "x = 1\n")
    _write(tmp_path, "mypkg/__init__.py", "")
    _write(tmp_path, "tests/test_app.py", "import pytest\nimport app\n")
    _write(tmp_path, ".venv/lib/site.py", "import numpy\n")
    assert set(py_deps.third_party_imports(tmp_path)) == {"requests", "bs4"}


def test_a_broken_file_is_skipped_not_fatal(tmp_path) -> None:
    _write(tmp_path, "bad.py", "def (:\n")
    _write(tmp_path, "ok.py", "import yaml\n")
    assert set(py_deps.third_party_imports(tmp_path)) == {"yaml"}


def test_undeclared_uses_pip_names_and_pep503(tmp_path) -> None:
    _write(tmp_path, "app.py", "import yaml\nimport docx\nimport dateutil.parser\nimport requests\n")
    _write(tmp_path, "requirements.txt", "# deps\nPyYAML>=6\npython_dateutil==2.9 ; python_version>'3'\n-e .\n")
    missing = {pip for _, pip, _ in py_deps.undeclared(tmp_path)}
    assert missing == {"python-docx", "requests"}


def test_pyproject_dependencies_count(tmp_path) -> None:
    pytest.importorskip("tomllib")
    _write(tmp_path, "app.py", "import openpyxl\nimport rich\n")
    _write(tmp_path, "pyproject.toml", "[project]\nname='x'\ndependencies=['openpyxl>=3']\n"
                                       "[project.optional-dependencies]\ncli=['rich']\n")
    assert py_deps.undeclared(tmp_path) == []


def test_nothing_declared_means_every_import_is_missing(tmp_path) -> None:
    _write(tmp_path, "app.py", "import openpyxl\n")
    assert py_deps.declared(tmp_path) is None
    assert [m for m, _, _ in py_deps.undeclared(tmp_path)] == ["openpyxl"]


@pytest.mark.parametrize(("output", "expect"), [
    ("ModuleNotFoundError: No module named 'docx'", "python -m pip install python-docx"),
    ("ModuleNotFoundError: No module named 'openpyxl'", "python -m pip install openpyxl"),
    ("ModuleNotFoundError: No module named 'PIL'", "`pillow`, не `PIL`"),
])
def test_missing_module_hint(tmp_path, output, expect) -> None:
    assert expect in py_deps.missing_module_hint(output, tmp_path)


def test_no_hint_for_local_or_stdlib_modules(tmp_path) -> None:
    _write(tmp_path, "egn.py", "")
    assert py_deps.missing_module_hint("No module named 'egn'", tmp_path) == ""
    assert py_deps.missing_module_hint("No module named 'tomllib'", tmp_path) == ""
    assert py_deps.missing_module_hint("all good", tmp_path) == ""


def test_hint_says_to_create_requirements_when_there_is_none(tmp_path) -> None:
    assert "създай requirements.txt" in py_deps.missing_module_hint("No module named 'requests'", tmp_path)
    _write(tmp_path, "requirements.txt", "")
    assert "добави в requirements.txt" in py_deps.missing_module_hint("No module named 'requests'", tmp_path)


def test_run_cmd_appends_the_hint(tmp_path) -> None:
    import sys

    import genesis_skills as gs
    gs.set_workspace(tmp_path)
    try:
        out = gs._tool_run_cmd(f'"{sys.executable}" -c "import surely_not_a_real_pkg_xyz"')
    finally:
        gs.set_workspace(gs._PROJECT_ROOT)
    assert "pip install surely_not_a_real_pkg_xyz" in out


def test_export_report_warns_about_undeclared_imports(tmp_path, monkeypatch) -> None:
    from genesis_agent import deliver
    from genesis_agent import workspace_memory as wm
    monkeypatch.setattr(wm, "DB_PATH", tmp_path / "wm.db")
    proj = tmp_path / "p"
    _write(proj, "main.py", "import openpyxl\nimport requests\n")
    _write(proj, "requirements.txt", "requests\n")
    report = deliver.export(proj, tmp_path / "o.zip", run_tests=False).report
    assert "`openpyxl` (`import openpyxl` в main.py)" in report
    assert "`requests` (" not in report


def test_equivalent_distributions_and_namespaces(tmp_path) -> None:
    """Одит 2026-10-07: декларираните psycopg2-binary, opencv-python-headless,
    google-cloud-storage, google-api-python-client се отчитаха като липсващи."""
    _write(tmp_path, "app.py", "import psycopg2\nimport cv2\nfrom google.cloud import storage\n"
                               "import googleapiclient.discovery\nfrom google.oauth2 import service_account\n")
    _write(tmp_path, "requirements.txt", "psycopg2-binary\nopencv-python-headless\n"
                                         "google-cloud-storage\ngoogle-api-python-client\n")
    assert [pip for _, pip, _ in py_deps.undeclared(tmp_path)] == ["google-auth"]


def test_a_bare_namespace_gets_no_wrong_hint(tmp_path) -> None:
    assert py_deps.missing_module_hint("No module named 'google'", tmp_path) == ""
    assert "google-cloud-storage" in py_deps.missing_module_hint(
        "No module named 'google.cloud.storage'", tmp_path)


def test_the_walk_does_not_descend_into_node_modules(tmp_path, monkeypatch) -> None:
    _write(tmp_path, "app.py", "import requests\n")
    _write(tmp_path, "node_modules/x/y.py", "import numpy\n")
    _write(tmp_path, ".venv/lib/z.py", "import pandas\n")
    assert set(py_deps.third_party_imports(tmp_path)) == {"requests"}
