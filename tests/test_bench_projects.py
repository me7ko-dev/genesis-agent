"""scripts/bench_projects.py — the parts that decide the numbers, without a model."""
import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("bench_projects", ROOT / "scripts" / "bench_projects.py")
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)


LOG = """\
  [Brain] ↪ модел: groq/openai/gpt-oss-120b
  [Brain] ✗ groq/openai/gpt-oss-120b след 0.3s: HTTP_429: {"error":"rate"} → следващ
  [Brain] ✗ nvidia/nvidia/nemotron-3-ultra-550b-a55b след 30.0s: timeout → следващ
↪ Отговорено от → ollama / gpt-oss:120b FREE (временно — пинът си остава groq/openai/gpt-oss-120b)
↪ Отговорено от → ollama / gpt-oss:120b FREE
  [Brain] ✗ groq/openai/gpt-oss-120b след 0.2s: HTTP_429: {} → следващ
"""


def test_parse_log_counts_drop_reasons():
    assert bp.parse_log(LOG) == {"HTTP_429": 2, "timeout": 1}


def test_parse_log_keeps_429s_with_a_retry_hint_in_one_bucket():
    # Brain пише `HTTP_429[retry=N]` от 2026-10-11 — всяко N беше отделен ключ.
    log = (LOG + "  [Brain] ✗ groq/a след 0.2s: HTTP_429[retry=2]: {} → следващ\n"
                 "  [Brain] ✗ groq/b след 0.2s: HTTP_429[retry=61]: {} → следващ\n")
    assert bp.parse_log(log) == {"HTTP_429": 4, "timeout": 1}


def test_usage_since_counts_every_call_including_the_pinned_model():
    lines = [
        '{"provider": "groq", "model": "openai/gpt-oss-120b", "total_tokens": 3000}',
        '{"provider": "groq", "model": "openai/gpt-oss-120b", "total_tokens": 1000}',
        '{"provider": "ollama", "model": "gpt-oss:120b", "total_tokens": 500}',
        'half a line from a crash',
    ]
    assert bp.usage_since(lines) == (4500, {"groq/openai/gpt-oss-120b": 2, "ollama/gpt-oss:120b": 1})


def test_parse_pytest_summary():
    assert bp.parse_pytest("..F.\n1 failed, 3 passed in 0.12s\n") == (3, 4)
    assert bp.parse_pytest("5 passed in 0.03s") == (5, 5)


def test_parse_pytest_collection_error_is_a_failure_not_zero_of_zero():
    out = "ERROR test_hidden.py - ModuleNotFoundError: No module named 'egn'\n1 error in 0.1s"
    assert bp.parse_pytest(out) == (0, 1)
    # the real output: "Interrupted: 1 error during collection" above the summary
    out = ("E   ModuleNotFoundError: No module named 'money'\n"
           "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!\n"
           "1 error in 0.17s\n")
    assert bp.parse_pytest(out) == (0, 1)
    assert bp.parse_pytest("") == (0, 1)


def test_summarize_and_table():
    runs = [
        {"project": "egn", "passed": 5, "total": 5, "seconds": 20, "tokens": 1000},
        {"project": "egn", "passed": 3, "total": 5, "seconds": 40, "tokens": 3000},
    ]
    s = bp.summarize(runs)["egn"]
    assert s["runs"] == 2 and s["ok"] == 1
    assert abs(s["tests"] - 0.8) < 1e-9
    assert s["seconds"] == 30 and s["tokens"] == 2000
    table = bp.format_table(bp.summarize(runs), before={"egn": {"ok": 0, "runs": 2, "tests": 0.5, "seconds": 60}})
    assert "1/2" in table and "0/2, 50%, 60 s" in table


def test_every_project_has_a_task_and_hidden_tests():
    projects = [p for p in bp.PROJECTS_DIR.iterdir() if p.is_dir()]
    assert projects
    for p in projects:
        assert (p / "task.txt").is_file(), p.name
        assert (p / "test_hidden.py").is_file(), p.name


def test_every_trial_gets_an_empty_memory_outside_its_folder(tmp_path):
    """2026-09-28 → 30: a site task's „preferences“ reached every later trial."""
    import sys
    workdir = tmp_path / "trial-1"
    workdir.mkdir()
    probe = ("import os, pathlib; m = pathlib.Path(os.environ['GENESIS_MEMORY_DIR']); "
             "print('MEM', m, sorted(p.name for p in m.iterdir())); (m / 'x.db').write_text('1')")
    log, _, stopped = bp.run_genesis([sys.executable, "-c", probe], "task", workdir, 60)
    assert stopped is None
    line = next(ln for ln in log.splitlines() if ln.startswith("MEM "))
    memory = Path(line.split(" ", 2)[1])
    assert line.endswith("[]")
    assert workdir not in memory.parents and tmp_path not in memory.parents
    assert not memory.exists()


_WITH_REFERENCE = sorted(p.name for p in bp.PROJECTS_DIR.iterdir() if (p / "reference").is_dir())


def test_the_new_projects_carry_a_reference_solution():
    assert len(_WITH_REFERENCE) >= 6


@pytest.mark.parametrize("name", _WITH_REFERENCE)
def test_hidden_tests_pass_against_the_reference(name, tmp_path):
    """A hidden test nobody can pass measures nothing: each project that ships
    a `reference/` solution must pass its own hidden tests with it — through
    the same `run_hidden` the benchmark uses. A package the project needs and
    this machine lacks (CI has no flask/bs4) skips, it does not fail."""
    import re
    import sys
    project = bp.PROJECTS_DIR / name
    workdir = tmp_path / f"{name}-1"
    shutil.copytree(project / "reference", workdir)
    passed, total, out = bp.run_hidden(sys.executable, project, workdir)
    missing = re.search(r"No module named '([\w.]+)'", out)
    if passed < total and missing and not (workdir / missing.group(1).split(".")[0]).exists() \
            and not (workdir / f"{missing.group(1).split('.')[0]}.py").exists():
        pytest.skip(f"{name}: needs {missing.group(1)}")
    assert passed == total and total > 1, out[-2000:]


# ── одит 2026-10-11: присъдата не бива да лъже ───────────────────────────────

def test_a_run_no_model_answered_is_infra_and_does_not_count():
    """Чат, паднал при внасяне, или без ключове: нито извикване в бюджетния
    лог, нито инструмент — това не е 0/1 на модела."""
    crashed = "Traceback ...\nModuleNotFoundError: No module named 'requests'\n"
    assert bp.is_infra({}, crashed)
    assert not bp.is_infra({"groq/x": 1}, crashed)
    assert not bp.is_infra({}, "╭─ 🔧 WRITE_FILE ─╮\n")      # отговор без usage
    runs = [{"project": "egn", "passed": 0, "total": 1, "seconds": 1, "tokens": 0, "infra": True},
            {"project": "egn", "passed": 5, "total": 5, "seconds": 20, "tokens": 900},
            {"project": "eik", "passed": 0, "total": 1, "seconds": 1, "tokens": 0, "infra": True}]
    s = bp.summarize(runs)
    assert s["egn"]["runs"] == 1 and s["egn"]["ok"] == 1 and s["egn"]["infra"] == 1
    assert s["eik"] == {"runs": 0, "ok": 0, "tests": 0.0, "seconds": 0.0, "tokens": 0.0, "infra": 1}
    table = bp.format_table(s)
    assert "без отговор от модела" in table and "1/1" in table


def test_a_hanging_hidden_test_is_a_failed_run_not_a_crash(tmp_path, monkeypatch):
    """Генериран модул с безкраен цикъл при внасяне: TimeoutExpired убиваше
    целия bench и губеше всички резултати."""
    import sys
    import time
    monkeypatch.setattr(bp, "HIDDEN_TIMEOUT", 3)
    project = tmp_path / "proj"
    project.mkdir()
    (project / "test_hidden.py").write_text("import egn\n\ndef test_x():\n    assert egn.ok()\n",
                                            encoding="utf-8")
    workdir = tmp_path / "proj-1"
    workdir.mkdir()
    (workdir / "egn.py").write_text("import time\nwhile True:\n    time.sleep(0.1)\n", encoding="utf-8")
    t0 = time.monotonic()
    passed, total, out = bp.run_hidden(sys.executable, project, workdir)
    assert time.monotonic() - t0 < 60
    assert passed == 0 and total >= 1
    assert "таймаут на скритите тестове" in out


def test_input_in_a_generated_module_does_not_wait_for_the_console():
    import sys
    rc, out = bp.run_capped([sys.executable, "-c", "input('?')"], cwd=None, env=None, timeout=30)
    assert rc not in (0, None) and "EOFError" in out


def test_missing_modules():
    import sys
    assert bp.missing_modules(sys.executable, ["json", "no_such_pkg_xyz"]) == ["no_such_pkg_xyz"]
    assert bp.missing_modules(sys.executable, []) == []
    assert bp.missing_modules(str(Path("no") / "such" / "python"), ["pytest"]) == ["pytest"]


def test_history_and_checkpoints_of_a_trial_do_not_stay(tmp_path):
    """Всеки пуск пишеше сесия в историята на оператора и ~200 KB снимка в
    GENESIS_HOME/checkpoints, които никой не чистеше."""
    import sys

    from genesis_agent import checkpoints
    workdir = tmp_path / "trial-1"
    workdir.mkdir()
    store = checkpoints._store(workdir.resolve())
    store.mkdir(parents=True)
    (store / "HEAD").write_text("x", encoding="utf-8")
    probe = ("import os, pathlib; h = pathlib.Path(os.environ['GENESIS_HISTORY_DIR']); "
             "print('HIST', h); (h / 's.json').write_text('{}')")
    log, _, _ = bp.run_genesis([sys.executable, "-c", probe], "task", workdir, 60)
    history = Path(next(ln for ln in log.splitlines() if ln.startswith("HIST ")).split(" ", 1)[1])
    assert not history.exists() and tmp_path not in history.parents
    assert not store.exists()


def _fake_chat(answer: dict):
    """run_genesis без модел: проект → папката, която „агентът“ написа, или
    None — чатът падна, нищо в бюджетния лог."""
    import json

    from genesis_agent import budget

    def run(cmd, task, workdir, timeout):
        name = workdir.name.rsplit("-", 1)[0]
        src = answer.get(name)
        if src is None:
            return "ModuleNotFoundError: No module named 'requests'\n", 1.0, None
        shutil.copytree(bp.PROJECTS_DIR / name / src, workdir, dirs_exist_ok=True)
        with Path(budget.LOG_PATH).open("a", encoding="utf-8") as f:
            f.write(json.dumps({"provider": "fake", "model": "m", "total_tokens": 1000}) + "\n")
        return "╭─ 🔧 WRITE_FILE ─╮\n", 2.0, None
    return run


def test_main_counts_a_crashed_chat_as_infra_not_a_failure(tmp_path, monkeypatch):
    import json
    import sys
    monkeypatch.setattr(bp, "run_genesis", _fake_chat({"sync-cli": "reference", "library-pkg": None}))
    out = tmp_path / "out"
    rc = bp.main(["--only", "sync-cli,library-pkg", "--out", str(out), "--test-python", sys.executable])
    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert data["complete"] is True
    assert data["summary"]["sync-cli"]["ok"] == 1 and data["summary"]["sync-cli"]["tokens"] == 1000
    assert data["summary"]["library-pkg"]["runs"] == 0 and data["summary"]["library-pkg"]["infra"] == 1
    assert rc == 1                                   # непълно пускане не е „всичко мина“


def test_results_are_kept_when_the_bench_stops_midway(tmp_path, monkeypatch):
    import json
    import sys
    chat = _fake_chat({"customers-sqlite": "reference"})

    def stop_on_second(cmd, task, workdir, timeout):
        if workdir.name.startswith("library-pkg"):
            raise KeyboardInterrupt
        return chat(cmd, task, workdir, timeout)
    monkeypatch.setattr(bp, "run_genesis", stop_on_second)
    out = tmp_path / "out"
    with pytest.raises(KeyboardInterrupt):
        bp.main(["--only", "customers-sqlite,library-pkg", "--out", str(out), "--test-python", sys.executable])
    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert data["complete"] is False and list(data["summary"]) == ["customers-sqlite"]


def _must_not_run(*_a):
    pytest.fail("bench не бива да пуска чат")


def test_a_reused_out_folder_is_refused_before_anything_runs(tmp_path, monkeypatch, capsys):
    import sys
    monkeypatch.setattr(bp, "run_genesis", _must_not_run)
    out = tmp_path / "out"
    (out / "sync-cli-1").mkdir(parents=True)
    assert bp.main(["--only", "sync-cli", "--out", str(out), "--test-python", sys.executable]) == 2
    assert "вече има пускане" in capsys.readouterr().out


def test_a_bad_compare_path_is_refused_before_anything_runs(tmp_path, monkeypatch, capsys):
    import sys
    monkeypatch.setattr(bp, "run_genesis", _must_not_run)
    rc = bp.main(["--only", "sync-cli", "--out", str(tmp_path / "o"), "--test-python", sys.executable,
                  "--compare", "~/no/such/results.json"])
    assert rc == 2 and "--compare" in capsys.readouterr().out


def test_a_test_python_without_pytest_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bp, "run_genesis", _must_not_run)
    rc = bp.main(["--only", "sync-cli", "--out", str(tmp_path / "o"),
                  "--test-python", str(tmp_path / "no-python")])
    assert rc == 2 and "pytest" in capsys.readouterr().out


def test_a_project_whose_hidden_tests_need_a_missing_package_is_skipped(tmp_path, monkeypatch, capsys):
    import json
    import sys
    monkeypatch.setattr(bp, "NEEDS", {"sync-cli": ("no_such_pkg_xyz",)})
    monkeypatch.setattr(bp, "run_genesis", _fake_chat({"customers-sqlite": "reference"}))
    out = tmp_path / "out"
    rc = bp.main(["--only", "sync-cli,customers-sqlite", "--out", str(out), "--test-python", sys.executable])
    data = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert list(data["summary"]) == ["customers-sqlite"]
    assert data["skipped"] == {"sync-cli": ["no_such_pkg_xyz"]}
    assert "sync-cli: пропуснат" in capsys.readouterr().out
    assert rc == 0


def test_needs_name_real_projects():
    assert set(bp.NEEDS) <= {p.name for p in bp.PROJECTS_DIR.iterdir() if p.is_dir()}
