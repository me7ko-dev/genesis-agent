"""scripts/bench_all.py — the one-command measure: plan, baselines, verdict."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "bench_all", Path(__file__).resolve().parent.parent / "scripts" / "bench_all.py")
ba = importlib.util.module_from_spec(_SPEC)
sys.modules["bench_all"] = ba  # dataclass-ите търсят модула си там
_SPEC.loader.exec_module(ba)  # type: ignore[union-attr]


def _results(path: Path, summary: dict, **extra) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary": summary, **extra}), encoding="utf-8")
    return path


def _s(ok: int, runs: int = 2, seconds: float = 100, tokens: float = 50_000) -> dict:
    return {"runs": runs, "ok": ok, "tests": ok / runs, "seconds": seconds, "tokens": tokens}


def test_the_newest_baseline_of_the_right_kind_is_found(tmp_path) -> None:
    import os
    old = _results(tmp_path / "2026-09-28" / "results.json", {"egn": _s(1)})
    new = _results(tmp_path / "2026-09-30-history" / "results.json", {"egn": _s(2)})
    _results(tmp_path / "fcc-x" / "results.json", {"c365": _s(0)}, fcc="abc")
    _results(tmp_path / "fixrun" / "results.json", {"median": _s(1, 1)}, runs=[{"fixed": True}])
    _results(tmp_path / "all-1" / "projects-acceptance" / "results.json", {"egn": _s(2)})
    os.utime(old, (1, 1))
    assert ba.find_baseline(tmp_path, "projects") == new
    assert ba.find_baseline(tmp_path, "fcc").parent.name == "fcc-x"
    assert ba.find_baseline(tmp_path, "fix").parent.name == "fixrun"
    assert ba.find_baseline(tmp_path, "projects", acceptance=True).parent.name == "projects-acceptance"
    assert ba.find_baseline(tmp_path, "projects", exclude=tmp_path / "2026-09-30-history") == old


@pytest.fixture(autouse=True)
def _no_fcc_challenges(tmp_path, monkeypatch):
    """Задачите на fCC — само където тестът ги сложи, не в дома на машината."""
    monkeypatch.setattr(ba, "fcc_dir", lambda: tmp_path / "genesis" / "fcc")


def test_fcc_reuses_the_baselines_challenges(tmp_path) -> None:
    home = tmp_path / "genesis" / "bench"
    (tmp_path / "genesis" / "fcc").mkdir(parents=True)
    base = _results(home / "fcc" / "results.json", {"c365": _s(0), "c023": _s(1)}, fcc="x")
    step = ba.plan(_ns(steps="fcc", baseline_fcc=base), tmp_path / "out", home)[0]
    assert not step.skip
    assert step.argv[step.argv.index("--only") + 1] == "365,23"
    assert step.argv[step.argv.index("--runs") + 1] == "1"


def test_fcc_is_skipped_without_the_challenges(tmp_path) -> None:
    steps = ba.plan(_ns(steps="fcc"), tmp_path / "out", tmp_path / "genesis" / "bench")
    assert [s.name for s in steps] == ["fcc"] and "bench_fcc.py --update" in steps[0].skip


def _ns(**kw):
    import argparse
    base = {"steps": ",".join(ba.STEPS), "runs": 2, "fcc_runs": 1, "quick": False, "fcc_only": "",
            **{f"baseline_{s}": None for s in ba.STEPS}}
    base.update(kw)
    return argparse.Namespace(**base)


def test_acceptance_is_compared_with_this_runs_projects(tmp_path) -> None:
    steps = ba.plan(_ns(), tmp_path / "out", tmp_path)
    acc = next(s for s in steps if s.name == "acceptance")
    assert acc.env == {"GENESIS_ACCEPTANCE": "1"}
    assert acc.baseline == tmp_path / "out" / "projects" / "results.json"
    assert [s.name for s in ba.plan(_ns(quick=True), tmp_path / "out", tmp_path)
            if not s.skip] == ["projects", "fix"]


def test_an_unknown_step_is_refused(tmp_path) -> None:
    with pytest.raises(SystemExit):
        ba.plan(_ns(steps="projects,bogus"), tmp_path, tmp_path)


def test_compare_names_regressions_and_improvements() -> None:
    before = {"egn": _s(2), "eik": _s(1), "iban": _s(2)}
    now = {"egn": _s(1), "eik": _s(2), "iban": _s(2), "car-ads": _s(2)}
    c = ba.compare(now, before)
    assert c["regressed"] == ["egn"] and c["improved"] == ["eik"] and c["new"] == ["car-ads"]
    assert c["before"]["runs"] == 6 and c["now_common"]["runs"] == 6


def test_verdict() -> None:
    ok = {"step": "projects", "status": "ok", "compare": ba.compare({"a": _s(2)}, {"a": _s(2)})}
    bad = {"step": "fix", "status": "ok", "compare": ba.compare({"a": _s(0, 1)}, {"a": _s(1, 1)})}
    acc = {"step": "acceptance", "status": "ok", "compare": ba.compare({"a": _s(2)}, {"a": _s(1)})}
    assert ba.verdict_of([ok]).startswith("✅")
    assert "РЕГРЕСИЯ" in ba.verdict_of([ok, bad])
    assert "не се пуснаха: fcc" in ba.verdict_of([ok, {"step": "fcc", "status": "timeout"}])
    assert "поправиха a" in ba.verdict_of([ok, acc])


def test_a_whole_run_with_fake_steps(tmp_path, monkeypatch) -> None:
    """Истинският main(): стъпките са малки скриптове, които пишат results.json."""
    base = _results(tmp_path / "home" / "old" / "results.json", {"egn": _s(2), "eik": _s(2)})

    def fake_plan(args, out, home):
        def step(name, summary, baseline):
            d = out / name
            code = (f"import json, pathlib; p = pathlib.Path({str(d)!r}); p.mkdir(parents=True); "
                    f"(p / 'results.json').write_text(json.dumps({{'summary': {summary!r}}}))")
            return ba.Step(name, [sys.executable, "-c", code], out=d, baseline=baseline)
        return [step("projects", {"egn": _s(2), "eik": _s(1)}, base),
                ba.Step("fix", [sys.executable, "-c", "raise SystemExit(3)"], out=out / "fix")]
    monkeypatch.setattr(ba, "plan", fake_plan)
    monkeypatch.setattr(ba, "bench_home", lambda: tmp_path / "home")
    rc = ba.main(["--out", str(tmp_path / "out")])
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert rc == 1
    assert "eik" in report and "РЕГРЕСИЯ" in report
    assert "без резултат (код 3)" in report
    assert json.loads((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))["steps"][0]["status"] == "ok"


def test_dry_run_runs_nothing(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(ba, "bench_home", lambda: tmp_path)
    assert ba.main(["--dry-run", "--out", str(tmp_path / "out")]) == 0
    assert not (tmp_path / "out").exists()
    assert "GENESIS_ACCEPTANCE=1" in capsys.readouterr().out


# ── одит 2026-10-11: присъдата ───────────────────────────────────────────────

def _infra(runs: int = 1) -> dict:
    return {"runs": 0, "ok": 0, "tests": 0.0, "seconds": 0.0, "tokens": 0.0, "infra": runs}


def _fake_steps(monkeypatch, tmp_path, steps: dict[str, tuple[dict | None, Path | None]], **extra) -> None:
    """plan() от стъпки, всяка пише {'summary': ..., **extra} — или нищо (None)."""
    def fake_plan(args, out, home):
        assert out.is_absolute()
        made = []
        for name, (summary, baseline) in steps.items():
            d = out / name
            data = {"summary": summary, **extra.get(name, {})}
            code = ("import json, pathlib, sys; p = pathlib.Path(sys.argv[1]); p.mkdir(parents=True); "
                    "(p / 'results.json').write_text(sys.argv[2])") if summary is not None else "pass"
            made.append(ba.Step(name, [sys.executable, "-c", code, str(d), json.dumps(data)],
                                out=d, baseline=baseline))
        return made
    monkeypatch.setattr(ba, "plan", fake_plan)
    monkeypatch.setattr(ba, "bench_home", lambda: tmp_path / "home")


def test_a_step_where_no_model_answered_is_not_a_regression(tmp_path, monkeypatch) -> None:
    """Грешен Python / без ключове: 0/16 и 0 токена — беше „❌ РЕГРЕСИЯ“ за
    всеки проект, зелен в базата."""
    base = _results(tmp_path / "home" / "old" / "results.json", {"egn": _s(2), "eik": _s(2)})
    _fake_steps(monkeypatch, tmp_path, {"projects": ({"egn": _infra(2), "eik": _infra(2)}, base)})
    rc = ba.main(["--out", str(tmp_path / "out")])
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert rc == 1
    assert "РЕГРЕСИЯ" not in report
    assert "моделът не отговори" in report


def test_some_runs_without_an_answer_make_the_verdict_incomplete(tmp_path, monkeypatch) -> None:
    base = _results(tmp_path / "home" / "old" / "results.json", {"egn": _s(2), "eik": _s(2)})
    _fake_steps(monkeypatch, tmp_path, {"projects": ({"egn": _s(2), "eik": _infra(2)}, base)})
    rc = ba.main(["--out", str(tmp_path / "out")])
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert rc == 1 and "⚠️ Непълно" in report and "eik" in report and "РЕГРЕСИЯ" not in report


def test_an_interrupted_step_is_not_compared(tmp_path, monkeypatch) -> None:
    base = _results(tmp_path / "home" / "old" / "results.json", {"egn": _s(2), "eik": _s(2)})
    _fake_steps(monkeypatch, tmp_path, {"projects": ({"egn": _s(2)}, base)},
                projects={"complete": False})
    assert ba.main(["--out", str(tmp_path / "out")]) == 1
    assert "прекъсната" in (tmp_path / "out" / "report.md").read_text(encoding="utf-8")


def test_a_used_out_folder_is_refused(tmp_path, monkeypatch, capsys) -> None:
    """Стъпка, паднала в стара папка, оставяше стария results.json — и той се
    отчиташе като сегашен, с ✅."""
    out = tmp_path / "out"
    _results(out / "projects" / "results.json", {"egn": _s(2)})
    _fake_steps(monkeypatch, tmp_path, {"projects": (None, None)})
    assert ba.main(["--out", str(out)]) == 2
    assert "не е празна" in capsys.readouterr().out
    assert not (out / "report.md").exists()


def test_a_relative_out_is_resolved_from_where_bench_all_was_started(tmp_path, monkeypatch) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    _fake_steps(monkeypatch, tmp_path, {"fix": ({"median": _s(1, 1)}, None)})
    assert ba.main(["--out", "relout"]) == 0
    assert "| fix | 1/1 |" in (elsewhere / "relout" / "report.md").read_text(encoding="utf-8")


def test_tilde_is_expanded(tmp_path, monkeypatch, capsys) -> None:
    """PowerShell 5.1 и cmd подават `~` буквално."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(ba, "bench_home", lambda: tmp_path / "home")
    assert ba.main(["--dry-run", "--steps", "fix", "--out", "~/benchx"]) == 0
    assert f"bench_all → {(tmp_path / 'benchx').resolve()}" in capsys.readouterr().out


def test_a_missing_explicit_baseline_is_an_error_not_no_baseline(tmp_path) -> None:
    with pytest.raises(SystemExit, match="baseline-fix"):
        ba.plan(_ns(steps="fix", baseline_fix=Path("~/nope/results.json")), tmp_path / "out", tmp_path)


def test_no_baseline_is_its_own_verdict(tmp_path, monkeypatch) -> None:
    _fake_steps(monkeypatch, tmp_path, {"fix": ({"median": _s(0, 1)}, None)})
    assert ba.main(["--out", str(tmp_path / "out")]) == 0
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "ℹ️ Без база — няма с какво да се сравни (fix)" in report
    assert "✅" not in report


def test_verdict_names_the_steps_without_a_baseline() -> None:
    ok = {"step": "projects", "status": "ok", "compare": ba.compare({"a": _s(2)}, {"a": _s(2)})}
    fresh = {"step": "fix", "status": "ok", "compare": ba.compare({"a": _s(1, 1)}, None)}
    v = ba.verdict_of([ok, fresh])
    assert v.startswith("✅") and "Без база — няма с какво да се сравни: fix" in v
    assert ba.exit_code([ok, fresh]) == 0


def test_acceptance_noise_is_not_a_code_regression() -> None:
    ok = {"step": "projects", "status": "ok", "compare": ba.compare({"a": _s(2)}, {"a": _s(2)})}
    acc = {"step": "acceptance", "status": "ok", "compare": ba.compare({"a": _s(1)}, {"a": _s(2)})}
    v = ba.verdict_of([ok, acc])
    assert "РЕГРЕСИЯ" not in v and "Приемните тестове влошиха: a" in v
    assert ba.exit_code([ok, acc]) == 0


def test_the_baseline_covers_the_plan_not_just_the_newest(tmp_path) -> None:
    """Едно `--only sync-cli` ставаше база на пълния bench; напълно счупено
    пускане (0 токена) или прекъснато — също."""
    import os
    full = _results(tmp_path / "2026-10-01" / "results.json", {"a": _s(2), "b": _s(2), "c": _s(2)})
    os.utime(full, (1, 1))
    _results(tmp_path / "only-a" / "results.json", {"a": _s(2)})
    want = {"a", "b", "c"}
    assert ba.find_baseline(tmp_path, "projects", want=want) == full
    _results(tmp_path / "broken" / "results.json", {k: _s(0, tokens=0) for k in want})
    _results(tmp_path / "infra" / "results.json", {k: _infra() for k in want})
    _results(tmp_path / "stopped" / "results.json", {k: _s(2) for k in want}, complete=False)
    assert ba.find_baseline(tmp_path, "projects", want=want) == full
    info = ba.describe_baseline(tmp_path / "only-a" / "results.json", want)
    assert "1 проекта" in info and "НЕ покрива 2" in info and "b, c" in info


def test_compare_ignores_projects_without_an_answer_on_either_side() -> None:
    c = ba.compare({"a": _infra(), "b": _s(2)}, {"a": _s(2), "b": _infra()})
    assert c["regressed"] == [] and c["improved"] == [] and c["infra"] == ["a"]


def test_steps_get_an_empty_stdin(tmp_path, monkeypatch) -> None:
    """CONFIRM в стъпка питаше във лога и чакаше конзолата завинаги."""
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        return type("P", (), {"returncode": 0})()
    monkeypatch.setattr(ba.subprocess, "run", fake_run)
    ba.run_step(ba.Step("fix", ["x"], out=tmp_path), tmp_path, None)
    assert seen["stdin"] is ba.subprocess.DEVNULL


def test_a_cp1251_stdout_does_not_crash(tmp_path, monkeypatch) -> None:
    import io
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1251"))
    monkeypatch.setattr(ba, "bench_home", lambda: tmp_path)
    assert ba.main(["--dry-run", "--out", str(tmp_path / "out")]) == 0
    sys.stdout.flush()
    assert "bench_all →" in raw.getvalue().decode("utf-8")


def test_the_header_comes_before_the_skipped_fcc_line(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(ba, "bench_home", lambda: tmp_path)
    ba.main(["--dry-run", "--steps", "fix,fcc", "--out", str(tmp_path / "out")])
    out = capsys.readouterr().out
    assert out.index("bench_all →") < out.index("пропусната")
