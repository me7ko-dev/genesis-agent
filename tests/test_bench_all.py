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


def test_fcc_reuses_the_baselines_challenges(tmp_path) -> None:
    home = tmp_path / "genesis" / "bench"
    (tmp_path / "genesis" / "fcc").mkdir(parents=True)
    base = _results(home / "fcc" / "results.json", {"c365": _s(0), "c023": _s(1)}, fcc="x")
    step = ba.plan(_ns(steps="fcc", baseline_fcc=base), tmp_path / "out", home)[0]
    assert step.argv[step.argv.index("--only") + 1] == "365,23"
    assert step.argv[step.argv.index("--runs") + 1] == "1"


def test_fcc_is_skipped_without_the_challenges(tmp_path) -> None:
    assert ba.plan(_ns(steps="fcc"), tmp_path / "out", tmp_path / "genesis" / "bench") == []


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
    assert [s.name for s in ba.plan(_ns(quick=True), tmp_path / "out", tmp_path)] == ["projects", "fix"]


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
