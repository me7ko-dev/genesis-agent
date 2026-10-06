"""scripts/bench_fix.py — the projects themselves, without a model."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("bench_fix", ROOT / "scripts" / "bench_fix.py")
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


@pytest.fixture(autouse=True)
def _this_python(monkeypatch):
    monkeypatch.setattr(bf, "project_python", lambda root=None: sys.executable)


def _write(tmp_path, name):
    _task, files = bf.PROJECTS[name]
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("name", sorted(bf.PROJECTS))
def test_every_planted_bug_fails_its_test(name, tmp_path):
    root = _write(tmp_path, name)
    if bf._missing(root, bf.NEEDS.get(name, ())):
        pytest.skip(f"needs {bf.NEEDS[name]}")
    assert not bf._run_test(root)


def test_the_dependency_project_is_fixable_and_found_as_pytest(tmp_path):
    """PR #30's regression: the project needs openpyxl, so its tests only run
    with the project's Python, and `genesis fix` must find them on its own."""
    pytest.importorskip("openpyxl")
    from genesis_agent.repo_map import detect_project
    root = _write(tmp_path, "excel_total")
    assert detect_project(root).test_command.endswith("-m pytest -q")
    report = root / "report.py"
    report.write_text(report.read_text("utf-8").replace("sum(p for _, _, p in rows)",
                                                        "sum(q * p for _, q, p in rows)"), encoding="utf-8")
    assert bf._run_test(root)


def test_a_missing_package_is_reported(tmp_path):
    assert bf._missing(tmp_path, ("json", "no_such_package_xyz")) == ["no_such_package_xyz"]
