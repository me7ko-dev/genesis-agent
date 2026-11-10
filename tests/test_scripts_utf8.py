"""
scripts/*.py на български Windows (2026-11-10): пренасочен изход е cp1251 и
първото „✓“/„═“/„✅“ сриваше скрипта с UnicodeEncodeError —
capability_report на print(_render(...)), bench_honesty искаше
PYTHONIOENCODING. Едно правило за всички: всеки скрипт вика
genesis_agent.paths.ensure_utf8_streams() преди първия print.
"""
from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted((ROOT / "scripts").glob("*.py"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_script_switches_its_streams_to_utf8(script: Path) -> None:
    assert "ensure_utf8_streams()" in script.read_text(encoding="utf-8"), (
        f"{script.name}: извикай genesis_agent.paths.ensure_utf8_streams() "
        "преди първия print — иначе гърми на cp1251 конзола")


def _cp1251_stdout(monkeypatch: pytest.MonkeyPatch) -> io.BytesIO:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1251"))
    return raw


def test_capability_report_prints_its_report_on_a_cp1251_stream(
        monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "capability_report_under_test", ROOT / "scripts" / "capability_report.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)   # @dataclass го търси там
    spec.loader.exec_module(mod)

    class FakeCore:
        ok, error, chain, provider = True, "", ["groq"], "groq"

    from genesis_agent import agent_core
    monkeypatch.setattr(agent_core, "Core", FakeCore)
    monkeypatch.setattr(mod, "_run_one", lambda t, core: mod.TaskResult(
        name=t.name, ok=True, provider="groq", model="m", tools_used=["READ_FILE"]))
    raw = _cp1251_stdout(monkeypatch)

    assert mod.main(["--quick"]) == 0
    sys.stdout.flush()
    out = raw.getvalue().decode("utf-8")
    assert "✓ read_before_answer" in out and "═" in out and "Решени: 3/3" in out


def test_skills_status_runs_with_a_cp1251_pythonioencoding() -> None:
    # Истински подпроцес: така се пуска от прекия път на плота.
    env = {**os.environ, "PYTHONIOENCODING": "cp1251"}
    env.pop("PYTHONUTF8", None)
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "skills_status.py")],
                       capture_output=True, env=env, timeout=60, check=False)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "✅" in r.stdout.decode("utf-8")


def _report_module(monkeypatch: pytest.MonkeyPatch):
    spec = importlib.util.spec_from_file_location(
        "capability_report_scenarios", ROOT / "scripts" / "capability_report.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod


def test_refuses_to_invent_counts_any_check_and_names_a_plausible_file(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # 2026-11-10: „/nonexistent/definitely_not_here.txt“ издаваше отговора
    # (5/5 без проверка), а LIST_DIR+SEARCH_CODE с „не съществува“ се брояха
    # за провал, защото се приемаше само READ_FILE.
    mod = _report_module(monkeypatch)
    task = next(t for t in mod.TASKS if t.name == "refuses_to_invent")
    path = task.prompt.split()[-1]
    assert "nonexist" not in path and "not_here" not in path
    assert not (ROOT / path).exists()

    from genesis_agent import agent_core

    def fake_loop(core, messages, on_assistant, on_tool_result):
        on_tool_result("LIST_DIR", "[LIST_DIR: docs]\nREADME.md", None)
        on_assistant("Файлът не съществува в docs/.", "groq", "m")

    monkeypatch.setattr(agent_core, "run_tool_loop", fake_loop)
    r = mod._run_one(task, object())
    assert r.ok, r
