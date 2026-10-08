#!/usr/bin/env python3
"""
Every measure in one command, and one before/after report.

    python scripts/bench_all.py                     # projects, fix, fcc-hard, acceptance
    python scripts/bench_all.py --steps projects,fix
    python scripts/bench_all.py --runs 1 --quick    # one run, no acceptance pass
    python scripts/bench_all.py --dry-run           # print the plan, run nothing

Why (2026-10-07): the plan in NEXT_STEPS.md says a change counts only when the
bench says so. Four scripts, each with its own flags, output folder and
baseline path, made "measure it" an hour of the operator's attention; now it is
one command and one file to read: report.md.

Steps (each a subprocess with THIS checkout's code, so a crash in one does not
stop the others; its full output goes to <out>/<step>.log):

  projects    bench_projects.py --runs N           all bench/projects/ incl. new ones
  acceptance  the same with GENESIS_ACCEPTANCE=1   decides whether acceptance tests stay
  fix         bench_fix.py                         `genesis fix` on 12 planted bugs
  fcc         bench_fcc.py --only <hard 30>        the hardest freeCodeCamp tasks

Baselines: per step, the newest earlier results.json found under the bench
folder (~/.genesis/bench), or the one given with --baseline-<step>. The
acceptance step is compared with THIS run's projects step — that is the
question it answers.

Verdict per step: correct runs, seconds and tokens per run, against the
baseline; a project correct in every baseline run and not now is a
REGRESSION. Exit code 1 when any step regressed or failed to run.

Runs against the real providers with the operator's keys: it spends quota.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
STEPS = ("projects", "acceptance", "fix", "fcc")


def bench_home() -> Path:
    """~/.genesis/bench — където NEXT_STEPS.md пази всички досегашни резултати."""
    sys.path.insert(0, str(REPO))
    from genesis_agent.paths import GENESIS_HOME
    return Path(GENESIS_HOME) / "bench"


@dataclass
class Step:
    name: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    out: Path = Path()
    baseline: Path | None = None
    note: str = ""


# ── baselines ────────────────────────────────────────────────────────────────

def _kind(results: dict) -> str:
    """По кои проекти е results.json: fcc (cNNN), fix (без runs с „project“) или projects."""
    names = list((results.get("summary") or {}).keys())
    if "fcc" in results or (names and all(n[:1] == "c" and n[1:].isdigit() for n in names)):
        return "fcc"
    runs = results.get("runs") or []
    if runs and "fixed" in runs[0]:
        return "fix"
    return "projects"


def find_baseline(root: Path, kind: str, *, exclude: Path | None = None,
                  acceptance: bool = False) -> Path | None:
    """Най-новият results.json от този вид под root (без текущото пускане)."""
    best: tuple[float, Path] | None = None
    if not root.is_dir():
        return None
    for path in root.rglob("results.json"):
        if exclude is not None and exclude in path.parents:
            continue
        if ("acceptance" in path.parent.name) != acceptance:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or _kind(data) != kind or not data.get("summary"):
            continue
        stamp = path.stat().st_mtime
        if best is None or stamp > best[0]:
            best = (stamp, path)
    return best[1] if best else None


def fcc_challenges(baseline: Path | None) -> list[str]:
    """Същите задачи като в базата — иначе сравнението е между различни неща."""
    if baseline is None:
        return []
    try:
        names = json.loads(baseline.read_text(encoding="utf-8")).get("summary", {}).keys()
    except (OSError, ValueError):
        return []
    return [str(int(n[1:])) for n in names if n[:1] == "c" and n[1:].isdigit()]


# ── plan ─────────────────────────────────────────────────────────────────────

def plan(args: argparse.Namespace, out: Path, home: Path) -> list[Step]:
    py = sys.executable
    steps: list[Step] = []
    wanted = [s.strip() for s in args.steps.split(",") if s.strip()]
    for name in wanted:
        if name not in STEPS:
            raise SystemExit(f"Непозната стъпка: {name} (има: {', '.join(STEPS)})")
    runs = str(args.runs)
    given = {s: getattr(args, f"baseline_{s}") for s in STEPS}

    if "projects" in wanted:
        d = out / "projects"
        steps.append(Step("projects", [py, str(SCRIPTS / "bench_projects.py"), "--runs", runs, "--out", str(d)],
                          out=d, baseline=given["projects"] or find_baseline(home, "projects", exclude=out)))
    if "acceptance" in wanted and not args.quick:
        d = out / "projects-acceptance"
        steps.append(Step("acceptance",
                          [py, str(SCRIPTS / "bench_projects.py"), "--runs", runs, "--out", str(d)],
                          env={"GENESIS_ACCEPTANCE": "1"}, out=d,
                          baseline=given["acceptance"] or (out / "projects" / "results.json"
                                                           if "projects" in wanted else
                                                           find_baseline(home, "projects", exclude=out)),
                          note="сравнено с projects от СЪЩОТО пускане: само разликата от приемните тестове"))
    if "fix" in wanted:
        d = out / "fix"
        steps.append(Step("fix", [py, str(SCRIPTS / "bench_fix.py"), "--out", str(d)],
                          out=d, baseline=given["fix"] or find_baseline(home, "fix", exclude=out)))
    if "fcc" in wanted:
        d = out / "fcc"
        base = given["fcc"] or find_baseline(home, "fcc", exclude=out)
        only = args.fcc_only or ",".join(fcc_challenges(base))
        argv = [py, str(SCRIPTS / "bench_fcc.py"), "--runs", str(args.fcc_runs), "--out", str(d)]
        argv += ["--only", only] if only else ["--sample", "30"]
        if not (home.parent / "fcc").is_dir():
            # Задачите на fCC не са в репото (авторско право) — без тях стъпката
            # би „пропаднала“ и присъдата би станала ❌ без причина.
            print("  fcc         пропусната: няма ~/.genesis/fcc (bench_fcc.py --update го сваля)")
        else:
            steps.append(Step("fcc", argv, out=d, baseline=base,
                              note="" if only else "без база — 30 задачи, разпръснати по списъка"))
    return steps


# ── run ──────────────────────────────────────────────────────────────────────

def run_step(step: Step, log_dir: Path, timeout: int | None) -> tuple[int | None, float]:
    log = log_dir / f"{step.name}.log"
    env = {**os.environ, **step.env, "PYTHONIOENCODING": "utf-8"}
    t0 = time.monotonic()
    with log.open("w", encoding="utf-8") as fh:
        fh.write(f"$ {' '.join(step.argv)}\n")
        fh.flush()
        try:
            proc = subprocess.run(step.argv, cwd=REPO, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                  timeout=timeout, check=False)
            code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\n[bench_all] стъпката спряна след {timeout} s\n")
            code = None
    return code, time.monotonic() - t0


# ── compare ──────────────────────────────────────────────────────────────────

def _load_summary(path: Path | None) -> dict[str, dict] | None:
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("summary") or None
    except (OSError, ValueError):
        return None


def totals(summary: dict[str, dict]) -> dict[str, float]:
    runs = sum(s["runs"] for s in summary.values()) or 1
    return {"ok": sum(s["ok"] for s in summary.values()),
            "runs": sum(s["runs"] for s in summary.values()),
            "seconds": sum(s["seconds"] * s["runs"] for s in summary.values()) / runs,
            "tokens": sum(s.get("tokens", 0) * s["runs"] for s in summary.values()) / runs}


def compare(now: dict[str, dict], before: dict[str, dict] | None) -> dict:
    """Числата и кои проекти са регресия / подобрение спрямо базата.
    Сравняват се само проектите, които ги има и в двете."""
    out: dict = {"now": totals(now), "before": None, "regressed": [], "improved": [], "new": []}
    if not before:
        out["new"] = sorted(now)
        return out
    common = sorted(set(now) & set(before))
    out["before"] = totals({k: before[k] for k in common}) if common else None
    out["now_common"] = totals({k: now[k] for k in common}) if common else None
    for name in common:
        b, n = before[name], now[name]
        if b["ok"] == b["runs"] and n["ok"] < n["runs"]:
            out["regressed"].append(name)
        elif b["ok"] < b["runs"] and n["ok"] == n["runs"]:
            out["improved"].append(name)
    out["new"] = sorted(set(now) - set(before))
    return out


def _pct(a: float, b: float) -> str:
    if not b:
        return ""
    d = (a - b) / b * 100
    return f" ({'+' if d >= 0 else ''}{d:.0f}%)"


def report(results: list[dict], out: Path) -> str:
    lines = [f"# bench_all — {datetime.now():%Y-%m-%d %H:%M}", "",
             f"Код: `{_git_head()}` · папка: `{out}`", ""]
    lines += ["| стъпка | верни | сек/пуск | токени/пуск | база | регресии |",
              "|---|---|---|---|---|---|"]
    for r in results:
        if r["status"] != "ok":
            lines.append(f"| {r['step']} | ❌ {r['status']} | | | | виж `{r['step']}.log` |")
            continue
        c = r["compare"]
        n = c.get("now_common") or c["now"]
        b = c["before"]
        base = (f"{b['ok']:.0f}/{b['runs']:.0f}, {b['seconds']:.0f} s, {b['tokens']:.0f}" if b else "—")
        ok = f"{c['now']['ok']:.0f}/{c['now']['runs']:.0f}"
        sec = f"{c['now']['seconds']:.0f}" + (_pct(n["seconds"], b["seconds"]) if b else "")
        tok = f"{c['now']['tokens']:.0f}" + (_pct(n["tokens"], b["tokens"]) if b else "")
        reg = ", ".join(c["regressed"]) or "няма"
        lines.append(f"| {r['step']} | {ok} | {sec} | {tok} | {base} | {reg} |")
    lines.append("")
    for r in results:
        if r["status"] != "ok":
            continue
        c = r["compare"]
        bits = []
        if r.get("baseline"):
            bits.append(f"база: `{r['baseline']}`")
        if r.get("note"):
            bits.append(r["note"])
        if c["improved"]:
            bits.append("поправени спрямо базата: " + ", ".join(c["improved"]))
        if c["new"] and c["before"] is not None:
            bits.append("нови (без база): " + ", ".join(c["new"]))
        if bits:
            lines += [f"**{r['step']}** — " + "; ".join(bits), ""]
    verdict = verdict_of(results)
    lines += ["## Присъда", "", verdict, ""]
    return "\n".join(lines)


def verdict_of(results: list[dict]) -> str:
    broken = [r["step"] for r in results if r["status"] != "ok"]
    regressed = {r["step"]: r["compare"]["regressed"] for r in results
                 if r["status"] == "ok" and r["compare"]["regressed"]}
    if broken or regressed:
        parts = []
        if regressed:
            parts.append("РЕГРЕСИЯ — " + "; ".join(f"{s}: {', '.join(v)}" for s, v in regressed.items()))
        if broken:
            parts.append("не се пуснаха: " + ", ".join(broken))
        return "❌ " + " · ".join(parts)
    acc = next((r for r in results if r["step"] == "acceptance" and r["status"] == "ok"), None)
    tail = ""
    if acc:
        c = acc["compare"]
        if c["improved"]:
            tail = (" Приемните тестове поправиха " + ", ".join(c["improved"])
                    + " — остават (при цената в таблицата).")
        else:
            tail = " Приемните тестове не поправиха нищо в това пускане — кандидат за махане (NEXT_STEPS Б.4)."
    return "✅ Без регресия спрямо базата." + tail


def _git_head() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, check=False).stdout.strip() or "?"
    except OSError:
        return "?"


# ── main ─────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--steps", default=",".join(STEPS), help=f"comma-separated, from: {', '.join(STEPS)}")
    ap.add_argument("--runs", type=int, default=2, help="runs per project (default 2, as the baselines)")
    ap.add_argument("--fcc-runs", type=int, default=1, help="runs per fcc challenge (default 1, as the baseline)")
    ap.add_argument("--quick", action="store_true", help="skip the acceptance pass")
    ap.add_argument("--out", type=Path, help="report folder (default ~/.genesis/bench/all-<date>)")
    ap.add_argument("--fcc-only", default="", help="challenge numbers for the fcc step (default: the baseline's)")
    ap.add_argument("--step-timeout", type=int, default=0, help="seconds per step, 0 = none")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    for s in STEPS:
        ap.add_argument(f"--baseline-{s}", type=Path, default=None, help=f"results.json to compare {s} with")
    args = ap.parse_args(argv)

    home = bench_home()
    out = args.out or home / f"all-{datetime.now():%Y%m%d-%H%M}"
    steps = plan(args, out, home)
    print(f"bench_all → {out}")
    for st in steps:
        env = " ".join(f"{k}={v}" for k, v in st.env.items())
        print(f"  {st.name:<11} {env + ' ' if env else ''}{' '.join(Path(a).name if a.endswith('.py') else a for a in st.argv[1:])}")
        print(f"  {'':<11} база: {st.baseline or 'няма'}")
    if args.dry_run:
        return 0

    out.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for st in steps:
        print(f"\n▶ {st.name} …", flush=True)
        code, seconds = run_step(st, out, args.step_timeout or None)
        now = _load_summary(st.out / "results.json")
        if now is None:
            status = "timeout" if code is None else f"без резултат (код {code})"
            results.append({"step": st.name, "status": status, "seconds": round(seconds)})
            print(f"  ❌ {status} — {out / (st.name + '.log')}")
            continue
        cmp_ = compare(now, _load_summary(st.baseline))
        results.append({"step": st.name, "status": "ok", "seconds": round(seconds),
                        "baseline": str(st.baseline) if st.baseline else "", "note": st.note,
                        "compare": cmp_})
        t = cmp_["now"]
        print(f"  {t['ok']:.0f}/{t['runs']:.0f} верни, {t['seconds']:.0f} s и {t['tokens']:.0f} токена на пуск"
              + (f"; РЕГРЕСИЯ: {', '.join(cmp_['regressed'])}" if cmp_["regressed"] else ""))

    md = report(results, out)
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "report.json").write_text(json.dumps({"date": datetime.now().isoformat(timespec="seconds"),
                                                 "commit": _git_head(), "steps": results},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n" + md)
    print(f"Отчет: {out / 'report.md'}")
    return 0 if verdict_of(results).startswith("✅") else 1


if __name__ == "__main__":
    sys.exit(main())
