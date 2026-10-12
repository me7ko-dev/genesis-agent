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
stop the others; its full output goes to <out>/<step>.log, its stdin is empty):

  projects    bench_projects.py --runs N           all bench/projects/ incl. new ones
  acceptance  the same with GENESIS_ACCEPTANCE=1   decides whether acceptance tests stay
  fix         bench_fix.py                         `genesis fix` on 13 planted bugs
  fcc         bench_fcc.py --only <hard 30>        the hardest freeCodeCamp tasks

Baselines: per step, an earlier complete results.json under the bench folder
(~/.genesis/bench) in which a model answered, covering the most of the planned
projects (newest among equals) — or the one given with --baseline-<step>. The
acceptance step is compared with THIS run's projects step — that is the
question it answers.

Verdict per step: correct runs, seconds and tokens per run, against the
baseline; a project correct in every baseline run and not now is a
REGRESSION. Runs in which no model answered are counted apart, never as a
regression. Projects measured in the baseline but not now (or skipped now
for a missing package) are named in the report and the verdict. Exit code 1
when any step regressed, failed to run or is incomplete, or nothing ran; 0 for no regression and for a first run without a baseline.

Runs against the real providers with the operator's keys: it spends quota.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
STEPS = ("projects", "acceptance", "fix", "fcc")
NO_ANSWER = "не се пусна: моделът не отговори"
sys.path.insert(0, str(REPO))


def bench_home() -> Path:
    """~/.genesis/bench — където NEXT_STEPS.md пази всички досегашни резултати."""
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
    skip: str = ""          # защо стъпката не се пуска (напр. няма задачите на fCC)
    baseline_info: str = ""  # колко проекта, от кога, кои липсват


# ── the other scripts' data ──────────────────────────────────────────────────

@cache
def _script(name: str):
    """Модул от scripts/ (bench_fix, bench_fcc) — за данните им, не за пускане."""
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(f"_bench_all_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def planned(kind: str) -> set[str] | None:
    """Проектите, които стъпката ще пусне — базата трябва да ги покрива."""
    if kind == "projects":
        return {p.name for p in (REPO / "bench" / "projects").iterdir() if (p / "task.txt").is_file()}
    if kind == "fix":
        try:
            return set(_script("bench_fix").PROJECTS)
        except Exception:  # без зависимостите на Genesis — без изискване за покритие
            return None
    return None


def fcc_dir() -> Path:
    """Където bench_fcc.py държи задачите — СЪЩИЯТ път, не изчислен наново
    (2026-10-11: bench_all гледаше GENESIS_HOME/fcc, bench_fcc — ~/.genesis/fcc)."""
    try:
        return Path(_script("bench_fcc").FCC_DIR)
    except Exception:
        return Path.home() / ".genesis" / "fcc"


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


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def answered(summary: dict[str, dict]) -> bool:
    """Има ли пуск, в който модел е отговорил. Напълно счупено пускане (0/16,
    0 токена — грешен Python, без ключове) ставаше база и следващото „поправяше“
    всичко, а истинските регресии после се криеха (одит 2026-10-11)."""
    return any(s.get("runs", 0) > 0 and s.get("tokens", 0) > 0 for s in summary.values())


def usable(data: dict | None) -> bool:
    """Става ли за база: има резюме, пускането е стигнало до края и модел е отговорил."""
    return bool(data and data.get("summary") and data.get("complete", True) is not False
                and answered(data["summary"]))


def find_baseline(root: Path, kind: str, *, exclude: Path | None = None,
                  acceptance: bool = False, want: set[str] | None = None) -> Path | None:
    """results.json от този вид под root (без текущото пускане), който покрива
    най-много от `want`, а между равните — най-новият.

    Само „най-новият“ (до 2026-10-11) правеше едно случайно `--only sync-cli`
    база на следващия пълен bench: сравняваше се 1 проект от 16 и регресия в
    library-pkg минаваше като „✅ Без регресия“."""
    best: tuple[int, float, Path] | None = None
    if not root.is_dir():
        return None
    for path in root.rglob("results.json"):
        if exclude is not None and exclude in path.parents:
            continue
        if ("acceptance" in path.parent.name) != acceptance:
            continue
        data = _read(path)
        if data is None or _kind(data) != kind or not usable(data):
            continue
        covered = set(data["summary"]) | set(data.get("skipped") or {})
        score = (len(want & covered) if want else 0, path.stat().st_mtime, path)
        if best is None or score[:2] > best[:2]:
            best = score
    return best[2] if best else None


def describe_baseline(path: Path | None, want: set[str] | None) -> str:
    """„16 проекта, 2026-09-30; липсват: x“ — за плана и за отчета."""
    if path is None:
        return "няма — няма с какво да се сравни"
    data = _read(path) or {}
    names = set(data.get("summary") or {})
    date = str(data.get("date") or "")[:10] or datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
    info = f"{len(names)} проекта, {date}"
    # Пропуснатите там заради липсващ пакет (NEEDS) ще се пропуснат и сега на
    # същата машина — не са дупка в базата, само се казват.
    skipped = set(data.get("skipped") or {})
    missing = sorted((want or set()) - names - skipped)
    if missing:
        info += f"; ⚠️ НЕ покрива {len(missing)} от плана: {', '.join(missing)}"
    if skipped & (want or set()):
        info += f"; пропуснати там (няма пакет): {', '.join(sorted(skipped & (want or set())))}"
    return info


def fcc_challenges(baseline: Path | None) -> list[str]:
    """Същите задачи като в базата — иначе сравнението е между различни неща."""
    if baseline is None:
        return []
    try:
        names = json.loads(baseline.read_text(encoding="utf-8")).get("summary", {}).keys()
    except (OSError, ValueError):
        return []
    return [str(int(n[1:])) for n in names if n[:1] == "c" and n[1:].isdigit()]


def _given(path: Path | None, step: str) -> Path | None:
    """Изрично дадена база: трябва да я има. Липсващият файл тихо ставаше „без
    база“, а отчетът пак печаташе пътя като използван (одит 2026-10-11)."""
    if path is None:
        return None
    path = path.expanduser().resolve()
    if not usable(_read(path)):
        raise SystemExit(f"--baseline-{step}: {path} — няма го или не става за база "
                         f"(без резюме, непълно или без нито един отговор от модел)")
    return path


# ── plan ─────────────────────────────────────────────────────────────────────

def plan(args: argparse.Namespace, out: Path, home: Path) -> list[Step]:
    py = sys.executable
    steps: list[Step] = []
    wanted = [s.strip() for s in args.steps.split(",") if s.strip()]
    for name in wanted:
        if name not in STEPS:
            raise SystemExit(f"Непозната стъпка: {name} (има: {', '.join(STEPS)})")
    runs = str(args.runs)
    given = {s: _given(getattr(args, f"baseline_{s}"), s) for s in STEPS if s in wanted}

    if "projects" in wanted:
        d = out / "projects"
        want = planned("projects")
        base = given["projects"] or find_baseline(home, "projects", exclude=out, want=want)
        steps.append(Step("projects", [py, str(SCRIPTS / "bench_projects.py"), "--runs", runs, "--out", str(d)],
                          out=d, baseline=base, baseline_info=describe_baseline(base, want)))
    if "acceptance" in wanted and not args.quick:
        d = out / "projects-acceptance"
        want = planned("projects")
        base = given["acceptance"] or (out / "projects" / "results.json" if "projects" in wanted else
                                       find_baseline(home, "projects", exclude=out, want=want))
        steps.append(Step("acceptance",
                          [py, str(SCRIPTS / "bench_projects.py"), "--runs", runs, "--out", str(d)],
                          env={"GENESIS_ACCEPTANCE": "1"}, out=d, baseline=base,
                          baseline_info=("projects от това пускане" if "projects" in wanted and not given["acceptance"]
                                         else describe_baseline(base, want)),
                          note="сравнено с projects от СЪЩОТО пускане: само разликата от приемните тестове"))
    if "fix" in wanted:
        d = out / "fix"
        want = planned("fix")
        base = given["fix"] or find_baseline(home, "fix", exclude=out, want=want)
        steps.append(Step("fix", [py, str(SCRIPTS / "bench_fix.py"), "--out", str(d)],
                          out=d, baseline=base, baseline_info=describe_baseline(base, want)))
    if "fcc" in wanted:
        d = out / "fcc"
        base = given["fcc"] or find_baseline(home, "fcc", exclude=out)
        only = args.fcc_only or ",".join(fcc_challenges(base))
        argv = [py, str(SCRIPTS / "bench_fcc.py"), "--runs", str(args.fcc_runs), "--out", str(d)]
        argv += ["--only", only] if only else ["--sample", "30"]
        # Задачите на fCC не са в репото (авторско право) — без тях стъпката
        # би „пропаднала“ и присъдата би станала ❌ без причина.
        skip = "" if fcc_dir().is_dir() else f"няма {fcc_dir()} (bench_fcc.py --update го сваля)"
        steps.append(Step("fcc", argv, out=d, baseline=base, skip=skip,
                          baseline_info=describe_baseline(base, None),
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
            # stdin е празен: стъпка, която би питала (CONFIRM в пясъчника),
            # пишеше въпроса в лога и чакаше на конзолата завинаги, без видим
            # въпрос (одит 2026-10-11) — сега отговорът е „не“ веднага.
            proc = subprocess.run(step.argv, cwd=REPO, env=env, stdin=subprocess.DEVNULL, stdout=fh,
                                  stderr=subprocess.STDOUT, timeout=timeout, check=False)
            code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\n[bench_all] стъпката спряна след {timeout} s\n")
            code = None
    return code, time.monotonic() - t0


def step_result(step: Step, code: int | None, seconds: float) -> dict:
    """Какво остави стъпката: резултат за сравнение или защо няма такъв."""
    base = {"step": step.name, "seconds": round(seconds)}
    data = _read(step.out / "results.json")
    if data is None or not data.get("summary"):
        return {**base, "status": "timeout" if code is None else f"без резултат (код {code})"}
    if data.get("complete") is False:
        # Записаното до спирането остава в results.json, но не е цялото
        # пускане — сравнение би показало липсващите като „няма регресия“.
        why = "таймаут" if code is None else f"код {code}"
        return {**base, "status": f"прекъсната ({why}) — непълни резултати в {step.name}/results.json"}
    now = data["summary"]
    t = totals(now)
    if not t["runs"] and t["infra"]:
        return {**base, "status": NO_ANSWER}
    return {**base, "status": "ok", "baseline": str(step.baseline) if step.baseline else "",
            "baseline_info": step.baseline_info, "note": step.note,
            "skipped": {str(k): list(v) for k, v in (data.get("skipped") or {}).items()},
            "compare": compare(now, _load_summary(step.baseline))}


def unmeasured(r: dict) -> list[str]:
    """Проектите, които сега не са мерени: пропуснати заради липсващ пакет
    (NEEDS) или ги има в базата, но не и сега. Сравнението гледа само общите,
    така че без този списък покритието падаше тихо, с „✅ Без регресия“
    (преглед 2026-10-11)."""
    skipped = r.get("skipped") or {}
    names = sorted(set(skipped) | set(r["compare"].get("gone") or []))
    return [n + (f" (няма {', '.join(skipped[n])})" if skipped.get(n) else "") for n in names]


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
            "tokens": sum(s.get("tokens", 0) * s["runs"] for s in summary.values()) / runs,
            "infra": sum(s.get("infra", 0) for s in summary.values())}


def compare(now: dict[str, dict], before: dict[str, dict] | None) -> dict:
    """Числата и кои проекти са регресия / подобрение спрямо базата.
    Сравняват се само проектите, които ги има и в двете. Проект без нито един
    пуск с отговор от модел (runs == 0) не е нито регресия, нито подобрение."""
    out: dict = {"now": totals(now), "before": None, "regressed": [], "improved": [], "new": [],
                 "gone": [], "infra": sorted(k for k, s in now.items() if s.get("infra"))}
    if not before:
        out["new"] = sorted(now)
        return out
    common = sorted(set(now) & set(before))
    out["before"] = totals({k: before[k] for k in common}) if common else None
    out["now_common"] = totals({k: now[k] for k in common}) if common else None
    for name in common:
        b, n = before[name], now[name]
        if not b["runs"] or not n["runs"]:
            continue
        if b["ok"] == b["runs"] and n["ok"] < n["runs"]:
            out["regressed"].append(name)
        elif b["ok"] < b["runs"] and n["ok"] == n["runs"]:
            out["improved"].append(name)
    out["new"] = sorted(set(now) - set(before))
    out["gone"] = sorted(set(before) - set(now))
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
        if r["step"] == "acceptance" and c["regressed"]:
            reg = "влошиха: " + reg   # шум между два пуска, не регресия на кода
        lines.append(f"| {r['step']} | {ok} | {sec} | {tok} | {base} | {reg} |")
    lines.append("")
    for r in results:
        if r["status"] != "ok":
            continue
        c = r["compare"]
        bits = []
        if r.get("baseline"):
            bits.append(f"база: `{r['baseline']}`" + (f" ({r['baseline_info']})" if r.get("baseline_info") else ""))
        else:
            bits.append("база: няма — няма с какво да се сравни")
        if r.get("note"):
            bits.append(r["note"])
        if c.get("infra"):
            bits.append("⚠️ моделът не отговори (не са броени): " + ", ".join(c["infra"]))
        if unmeasured(r):
            bits.append("⚠️ не са мерени сега: " + ", ".join(unmeasured(r)))
        if c["improved"]:
            bits.append("поправени спрямо базата: " + ", ".join(c["improved"]))
        if c["new"] and c["before"] is not None:
            bits.append("нови (без база): " + ", ".join(c["new"]))
        lines += [f"**{r['step']}** — " + "; ".join(bits), ""]
    verdict = verdict_of(results)
    lines += ["## Присъда", "", verdict, ""]
    return "\n".join(lines)


def _acceptance_tail(results: list[dict]) -> str:
    acc = next((r for r in results if r["step"] == "acceptance" and r["status"] == "ok"), None)
    if not acc:
        return ""
    c = acc["compare"]
    tail = ""
    if c["improved"]:
        tail += (" Приемните тестове поправиха " + ", ".join(c["improved"])
                 + " — остават (при цената в таблицата).")
    if c["regressed"]:
        # Пропуснат пуск на runs=2 е обичаен шум на модела, не регресия на
        # кода — до 2026-10-11 това даваше „❌ РЕГРЕСИЯ — acceptance“ и код 1.
        tail += " Приемните тестове влошиха: " + ", ".join(c["regressed"]) + " (не е регресия на кода)."
    if not tail:
        tail = " Приемните тестове не поправиха нищо в това пускане — кандидат за махане (NEXT_STEPS Б.4)."
    return tail


def verdict_of(results: list[dict]) -> str:
    """❌ регресия или стъпка без резултат · ⚠️ непълно (моделът не отговори в
    някои пускове) · ℹ️ без база · ✅ без регресия. Само ✅ и ℹ️ дават код 0."""
    if not results:
        # `--steps fcc` без задачите: нищо не се пусна, а присъдата беше „ℹ️ Без
        # база (). Това пускане става база…“ с код 0 (преглед 2026-10-11).
        return "❌ Нищо не се пусна — всички стъпки са пропуснати; няма какво да се сравни."
    broken = [r["step"] + (" (моделът не отговори)" if r["status"] == NO_ANSWER else "")
              for r in results if r["status"] != "ok"]
    done = [r for r in results if r["status"] == "ok"]
    regressed = {r["step"]: r["compare"]["regressed"] for r in done
                 if r["step"] != "acceptance" and r["compare"]["regressed"]}
    infra = {r["step"]: r["compare"]["infra"] for r in done if r["compare"].get("infra")}
    infra_s = "; ".join(f"{s}: {', '.join(v)}" for s, v in infra.items())
    gaps = "; ".join(f"{r['step']}: {', '.join(unmeasured(r))}" for r in done if unmeasured(r))
    tail = (f" ⚠️ Не са мерени сега: {gaps}." if gaps else "") + _acceptance_tail(results)
    if broken or regressed:
        parts = []
        if regressed:
            parts.append("РЕГРЕСИЯ — " + "; ".join(f"{s}: {', '.join(v)}" for s, v in regressed.items()))
        if broken:
            parts.append("не се пуснаха: " + ", ".join(broken))
        if infra:
            parts.append("моделът не отговори в: " + infra_s)
        return "❌ " + " · ".join(parts) + tail
    if infra:
        return ("⚠️ Непълно — моделът не отговори в: " + infra_s
                + ". Тези пускове не са броени; пусни ги пак, преди да вярваш на присъдата." + tail)
    # acceptance не казва нищо за регресия на кода (виж _acceptance_tail), а
    # „базата“ ѝ обикновено е projects от същото пускане: първото пускане без
    # нито една база излизаше „✅ Без регресия спрямо базата (acceptance)“
    # (измерено 2026-10-11 с фалшив доставчик). Брои се само ако е сама.
    code_steps = [r for r in done if r["step"] != "acceptance"] or done
    compared = [r["step"] for r in code_steps if r["compare"]["before"] is not None]
    no_base = [r["step"] for r in code_steps if r["compare"]["before"] is None]
    if not compared:
        return ("ℹ️ Без база — няма с какво да се сравни (" + ", ".join(no_base)
                + "). Това пускане става база за следващото." + tail)
    head = "✅ Без регресия спрямо базата"
    if no_base:
        head += f" ({', '.join(compared)}). Без база — няма с какво да се сравни: {', '.join(no_base)}"
    return head + "." + tail


def exit_code(results: list[dict]) -> int:
    return 0 if verdict_of(results).startswith(("✅", "ℹ️")) else 1


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
    ap.add_argument("--out", type=Path, help="report folder, new or empty (default ~/.genesis/bench/all-<date>)")
    ap.add_argument("--fcc-only", default="", help="challenge numbers for the fcc step (default: the baseline's)")
    ap.add_argument("--step-timeout", type=int, default=0, help="seconds per step, 0 = none")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    for s in STEPS:
        ap.add_argument(f"--baseline-{s}", type=Path, default=None, help=f"results.json to compare {s} with")
    args = ap.parse_args(argv)

    from genesis_agent.paths import ensure_utf8_streams

    # Пренасочен stdout (`> bench.txt`, Tee-Object) на български Windows е
    # cp1251: „→“ в първия ред сриваше всичко преди първата стъпка (2026-10-11).
    ensure_utf8_streams()
    home = bench_home().expanduser().resolve()
    # Абсолютен път: стъпките тръгват с cwd=REPO, а bench_all четеше
    # относителния --out от своя cwd — „без резултат“ при успешна стъпка.
    # `~` не се разгъва от PowerShell 5.1 и cmd (одит 2026-10-11).
    out = (args.out.expanduser().resolve() if args.out else
           home / f"all-{datetime.now():%Y%m%d-%H%M%S}")
    steps = plan(args, out, home)
    print(f"bench_all → {out}")
    for st in steps:
        if st.skip:
            print(f"  {st.name:<11} пропусната: {st.skip}")
            continue
        env = " ".join(f"{k}={v}" for k, v in st.env.items())
        print(f"  {st.name:<11} {env + ' ' if env else ''}{' '.join(Path(a).name if a.endswith('.py') else a for a in st.argv[1:])}")
        print(f"  {'':<11} база: {st.baseline or 'няма'}" + (f" ({st.baseline_info})" if st.baseline else ""))
    # Стара папка: стъпка, паднала в нея, оставяше results.json от предишното
    # пускане и той се отчиташе като сегашен, с ✅ (одит 2026-10-11).
    # Файл вместо папка даваше NotADirectoryError traceback (преглед 2026-10-11).
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print(f"\n❌ {out} вече съществува и не е празна папка — дай нова --out.")
        return 2
    if args.dry_run:
        return 0

    out.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for st in steps:
        if st.skip:
            continue
        print(f"\n▶ {st.name} …", flush=True)
        code, seconds = run_step(st, out, args.step_timeout or None)
        res = step_result(st, code, seconds)
        results.append(res)
        if res["status"] != "ok":
            print(f"  ❌ {res['status']} — {out / (st.name + '.log')}")
            continue
        cmp_ = res["compare"]
        t = cmp_["now"]
        print(f"  {t['ok']:.0f}/{t['runs']:.0f} верни, {t['seconds']:.0f} s и {t['tokens']:.0f} токена на пуск"
              + (f"; без отговор от модела: {', '.join(cmp_['infra'])}" if cmp_["infra"] else "")
              + (f"; РЕГРЕСИЯ: {', '.join(cmp_['regressed'])}"
                 if cmp_["regressed"] and st.name != "acceptance" else ""))

    md = report(results, out)
    (out / "report.md").write_text(md, encoding="utf-8")
    (out / "report.json").write_text(json.dumps({"date": datetime.now().isoformat(timespec="seconds"),
                                                 "commit": _git_head(), "steps": results},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n" + md)
    print(f"Отчет: {out / 'report.md'}")
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
