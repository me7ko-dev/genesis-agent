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


def test_results_json_has_the_shape_bench_all_compares(tmp_path) -> None:
    import json
    results = [{"name": "median", "fixed": True, "reported": True, "rounds": 2, "test_touched": False,
                "sec": 4.0, "prompt": 5000, "completion": 900},
               {"name": "cache", "fixed": False, "reported": True, "rounds": 6, "test_touched": False,
                "sec": 30.0, "prompt": 9000, "completion": 1000}]
    path = bf.write_results(tmp_path / "fix", results, "")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["summary"]["median"] == {"runs": 1, "ok": 1, "tests": 1.0, "seconds": 4.0, "tokens": 5900,
                                         "false_claim": False, "test_touched": False, "infra": 0}
    assert data["summary"]["cache"]["false_claim"] is True
    assert data["runs"][0]["fixed"] is True             # bench_all разпознава вида по това
    assert data["complete"] is True


# ── одит 2026-10-11 ──────────────────────────────────────────────────────────

def _fixing_repair(calls: int = 1):
    """repair() без модел: поправя median и „вика“ модела `calls` пъти."""
    def repair(root, task, **_kw):
        bf.USAGE["calls"] += calls
        bf.USAGE["prompt"] += 100 * calls
        if calls and (root / "stats.py").exists():
            (root / "stats.py").write_text(
                "def median(xs):\n    s = sorted(xs)\n    n = len(s)\n"
                "    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2\n", encoding="utf-8")
        return type("Out", (), {"success": bool(calls), "rounds": 1})()
    return repair


def test_only_names_are_stripped_and_checked_before_any_project(monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(bf, "_load_agent", lambda model: _fixing_repair())
    monkeypatch.setattr(bf, "bench_one", lambda name, *a: ran.append(name))
    assert bf.main(["--only", "median,bogus"]) == 2
    assert ran == [] and "bogus" in capsys.readouterr().out
    done = {"fixed": True, "reported": True, "rounds": 1, "test_touched": False, "sec": 1.0,
            "prompt": 1, "completion": 1, "infra": False}
    monkeypatch.setattr(bf, "bench_one", lambda name, *a: ran.append(name) or {**done, "name": name})
    assert bf.main(["--only", "median, pagination"]) == 0          # интервал след запетаята
    assert ran == ["median", "pagination"]


def test_a_fix_that_hangs_is_a_failed_fix_not_a_crash(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(bf, "TEST_TIMEOUT", 3)
    (tmp_path / bf.TEST).write_text("while True:\n    pass\n", encoding="utf-8")
    t0 = time.monotonic()
    assert bf._run_test(tmp_path) is False
    assert time.monotonic() - t0 < 60


def test_no_model_answer_is_infra_not_a_failed_fix(tmp_path, monkeypatch, capsys) -> None:
    import json
    _task, files = bf.PROJECTS["median"]
    r = bf.bench_one("median", "x", files, 2, _fixing_repair(calls=0))
    assert r["infra"] is True and r["fixed"] is False
    s = bf.summarize([r])["median"]
    assert s["runs"] == 0 and s["ok"] == 0 and s["infra"] == 1 and s["false_claim"] is False
    assert bf._run(["median"], 2, "", tmp_path / "out", _fixing_repair(calls=0)) == 1
    assert "моделът не отговори" in capsys.readouterr().out
    data = json.loads((tmp_path / "out" / "results.json").read_text(encoding="utf-8"))
    assert data["summary"]["median"]["infra"] == 1


def test_results_survive_a_stop_after_the_first_project(tmp_path) -> None:
    import json

    def repair(root, task, **kw):
        if not (root / "stats.py").exists():
            raise KeyboardInterrupt
        return _fixing_repair()(root, task, **kw)
    with pytest.raises(KeyboardInterrupt):
        bf._run(["median", "pagination"], 2, "", tmp_path / "out", repair)
    data = json.loads((tmp_path / "out" / "results.json").read_text(encoding="utf-8"))
    assert data["complete"] is False and data["summary"]["median"]["ok"] == 1


def test_exit_code_says_whether_everything_was_fixed(tmp_path) -> None:
    assert bf._run(["median"], 2, "", None, _fixing_repair()) == 0
    assert bf._run(["median", "average"], 2, "", None, _fixing_repair()) == 1


def test_a_cp1251_stdout_does_not_crash_the_report(monkeypatch) -> None:
    """Пренасочен stdout на български Windows: първото „✅“ сриваше bench-а."""
    import io
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1251"))
    monkeypatch.setattr(bf, "_load_agent", lambda model: _fixing_repair())
    assert bf.main(["--only", "median"]) == 0
    sys.stdout.flush()
    assert "✅" in raw.getvalue().decode("utf-8")


def test_a_confirm_is_refused_even_with_a_console_on_stdin(monkeypatch) -> None:
    """Под bench_all stdin е конзолата: CONFIRM питаше във fix.log и чакаше вечно."""
    from genesis_agent import repo_agent, sandbox
    monkeypatch.setattr(sandbox, "_POLICY", sandbox._POLICY)
    monkeypatch.setattr(repo_agent, "Brain", repo_agent.Brain)
    monkeypatch.setattr(sys, "stdin", type("Tty", (), {"isatty": lambda self: True})())
    assert bf._load_agent("") is repo_agent.repair
    assert sandbox.get_policy().resolve_mode() == "deny"
    assert repo_agent.Brain.__name__ == "_Counting"


def test_repair_in_the_bench_gets_its_own_memory() -> None:
    """repair() в процеса на bench-а пишеше епизоди в episodes.db на чекаута."""
    import os
    import subprocess
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import bench_fix as bf; "
            "m = bf._isolate_memory(); from genesis_agent import config; "
            "print('SAME' if config.MEMORY_DIR == m else 'OTHER', m)")
    env = {k: v for k, v in os.environ.items() if k != "GENESIS_MEMORY_DIR"}
    out = subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts")], cwd=ROOT, env=env,
                         capture_output=True, text=True, timeout=120, check=False)
    memory = out.stdout.strip().partition(" ")[2]
    if memory and memory != "None":
        import shutil
        shutil.rmtree(memory, ignore_errors=True)
    assert out.stdout.startswith("SAME "), out.stdout + out.stderr


def test_a_touched_test_keeps_the_columns() -> None:
    base = {"rounds": 2, "sec": 3.0, "prompt": 10, "completion": 5, "reported": True}
    touched = bf._line({**base, "name": "sort_desc", "fixed": False, "test_touched": True})
    fixed = bf._line({**base, "name": "median", "fixed": True, "test_touched": False})
    assert touched.split()[1] == "sort_desc" and fixed.split()[1] == "median"
    assert "пипна теста" in touched and "каза „поправено“, не е" in touched
