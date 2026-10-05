"""scripts/bench_fix.py — the harness itself, without a model."""
import importlib.util
import subprocess
import sys
from pathlib import Path

from genesis_agent import repo_map

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("bench_fix", ROOT / "scripts" / "bench_fix.py")
assert _spec and _spec.loader
bf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)


def _run(cmd: str, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True,
                          timeout=120, check=False)


def test_venv_project_tests_run_in_its_own_venv(tmp_path) -> None:
    """NEXT_STEPS А.3, регресия за paths.project_python (PR #30): зависимостта
    е само в .venv на проекта. Откритата тестова команда я вижда и пада на
    засадения бъг, не на импорта; с Python-а на Genesis — пада на импорта."""
    _task, files = bf.PROJECTS["venv_dep"]
    root, python = bf.make_project(tmp_path / "p", "venv_dep", files)
    assert ".venv" in python
    cmd = repo_map.detect_project(root).test_command
    assert ".venv" in cmd

    own = subprocess.run([sys.executable, bf.TEST], cwd=root, capture_output=True, text=True,
                         timeout=60, check=False)
    assert "No module named 'benchmoney'" in own.stderr

    bug = _run(cmd, root)
    assert bug.returncode != 0 and "No module named" not in bug.stdout + bug.stderr
    assert not bf._run_test(root, python)

    src = root / "invoice.py"
    src.write_text(src.read_text(encoding="utf-8").replace(
        "to_money(net * rate / 100)", "to_money(net + net * rate / 100)"), encoding="utf-8")
    assert _run(cmd, root).returncode == 0
    assert bf._run_test(root, python)


def test_plain_project_uses_the_bench_python(tmp_path) -> None:
    _task, files = bf.PROJECTS["median"]
    root, python = bf.make_project(tmp_path / "m", "median", files)
    assert python == sys.executable and not (root / ".venv").exists()
