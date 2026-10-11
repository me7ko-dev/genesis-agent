#!/usr/bin/env python3
"""
Benchmark for what Genesis CREATES: every project in bench/projects/, judged
by hidden acceptance tests the agent never sees.

    python scripts/bench_projects.py                        # all projects, 1 run each
    python scripts/bench_projects.py --runs 3               # 3 runs per project
    python scripts/bench_projects.py --only egn-check,workdays
    python scripts/bench_projects.py --compare old/results.json   # before → after

Why this exists (NEXT_STEPS.md, plan A.1, 2026-09-25): Genesis's own tests are
not a measure — they were green while ЕГН, ЕИК and the euro were wrong. Until
now each trial was run by hand (bench/projects/README.md). One command makes
"did this change help?" a number instead of a guess.

Each run: a fresh empty folder, the chat started there with the task on stdin
and `изход` after it, exactly as the operator would type it. Then the
project's `test_hidden.py` is copied OUTSIDE that folder and run against it
with the machine's own Python (the one Genesis runs the user's tests with).
Recorded per run: hidden tests passed/total, wall time, tokens and calls per
model from the budget log, and why models dropped out (the `✗` lines).

A run in which no model answered (no call in the budget log, no tool used —
the chat died at import, or no key was reachable) is marked `infra` and
counted apart: it says nothing about the agent.

Run it with an interpreter that has Genesis's dependencies (the pipx venv's
python.exe on the laptop). The code under test is THIS checkout, not the
installed copy. It runs against the real providers: it spends free quota.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROJECTS_DIR = REPO / "bench" / "projects"
sys.path.insert(0, str(REPO))

# `[` също слага край на причината (2026-10-11): Brain пише `HTTP_429[retry=N]`
# и всяко N иначе ставаше отделен ключ — 429-ите в отчета се разпиляваха.
_DROPPED = re.compile(r"\[Brain\] ✗ (\S+) след [\d.]+s: (\S+?)[:\s\[]")
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")
_TOOL = re.compile(r"🔧 ([A-Z_]+) ")
# A chat stuck printing the same error fills the disk long before the timeout:
# measured 2026-09-26, 1.2M lines in 5 minutes. A real run logs well under 1 MB.
MAX_LOG_BYTES = 20_000_000
# Таван за скритите тестове на един пуск. Генериран модул, който блокира при
# внасяне (Flask app.run() без __main__, input(), безкраен цикъл), убиваше
# целия bench с TimeoutExpired и губеше всички резултати (одит 2026-10-11).
HIDDEN_TIMEOUT = 300

# Пакети, които САМИТЕ скрити тестове внасят (не решението на агента) и които
# трябва да ги има в Python-а за скритите тестове. Без тях и еталонното решение
# пада с „No module named flask“ — 0/1, отчетено като грешка на модела, а
# bench_all го превръщаше в РЕГРЕСИЯ (одит 2026-10-11). Такъв проект се
# пропуска с ясна причина, както NEEDS в bench_fix.py.
NEEDS: dict[str, tuple[str, ...]] = {
    "booking-form": ("flask",),
    "car-ads": ("bs4",),
    "faktura-excel": ("reportlab",),
    "tasks-api": ("flask",),
}


def parse_log(text: str) -> dict[str, int]:
    """Why models dropped out of the chain (the `✗` lines), by reason."""
    return dict(Counter(reason for _model, reason in _DROPPED.findall(text)))


def tool_counts(log: str) -> dict[str, int]:
    """Кои инструменти е викал пускът — по заглавията `🔧 ИМЕ` в лога на чата."""
    return dict(Counter(_TOOL.findall(log)))


def is_infra(models: dict[str, int], log: str) -> bool:
    """Пускът не стигна до модел: нито едно записано извикване в бюджетния лог
    и нито един инструмент. Чат, паднал при внасяне (грешен Python, липсващ
    пакет), или без нито един ключ се броеше за 0/1 — грешка на модела — и
    bench_all обявяваше РЕГРЕСИЯ за всеки проект (одит 2026-10-11). Такъв пуск
    не се брои нито за верен, нито за грешен."""
    return not models and not tool_counts(log)


def parse_pytest(output: str) -> tuple[int, int]:
    """(passed, total) from pytest's summary line. A collection error — the
    module is missing or does not import — counts as one failed test, so a
    project that produced nothing never reads as 0/0 "nothing failed"."""
    # Only the final summary ("1 failed, 3 passed in 0.12s"): a collection
    # error also prints "Interrupted: 1 error during collection" above it.
    summary = next((ln for ln in reversed(output.splitlines())
                    if _PYTEST_COUNT.search(ln) and re.search(r"\bin [\d.]+s\b", ln)), output)
    counts = {"passed": 0, "failed": 0, "error": 0}
    for n, kind in _PYTEST_COUNT.findall(summary):
        counts["error" if kind.startswith("error") else kind] += int(n)
    total = counts["passed"] + counts["failed"] + counts["error"]
    if total == 0:
        return 0, 1
    return counts["passed"], total


def usage_since(lines: list[str]) -> tuple[int, dict[str, int]]:
    """(tokens, calls per model) from budget-log lines. The budget log, not the
    chat log: the chat prints "Отговорено от" only when a model OTHER than the
    pinned one answered, so the pinned model's calls would be invisible there."""
    tokens, models = 0, Counter()
    for line in lines:
        try:
            rec = json.loads(line)
            tokens += int(rec.get("total_tokens") or 0)
            models[f"{rec['provider']}/{rec['model']}"] += 1
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return tokens, dict(models)


def _log_lines_since(log_path: Path, offset: int) -> list[str]:
    try:
        with log_path.open("rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, timeout=15, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()


def run_capped(cmd: list[str], *, cwd: Path | None, env: dict[str, str] | None,
               timeout: float) -> tuple[int | None, str]:
    """(код, изход) на команда с таван; код None = спряна след `timeout`.
    Убива се цялото дърво процеси: subprocess.run(timeout=) на Windows убива
    само прякото дете и сървърът, пуснат от теста, оставаше жив (2026-10-11).
    stdin е празен — input() в генериран модул не чака вечно."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=os.name != "nt")
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:  # внук, излязъл от групата, държи тръбата
            out = b""
        return None, out.decode("utf-8", "replace")


def missing_modules(python: str, modules: tuple[str, ...] | list[str]) -> list[str]:
    """Кои от `modules` липсват в `python`. Интерпретатор, който изобщо не
    тръгва, няма нито един от тях."""
    if not modules:
        return []
    probe = ("import importlib.util, sys; "
             "print('@@' + ','.join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))")
    try:
        rc, out = run_capped([python, "-c", probe, *modules], cwd=None, env=None, timeout=60)
    except OSError:
        return list(modules)
    line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("@@")), None)
    if rc != 0 or line is None:
        return list(modules)
    return [m for m in line[2:].split(",") if m]


def run_genesis(genesis_cmd: list[str], task: str, workdir: Path, timeout: int) -> tuple[str, float, str | None]:
    """Chat in `workdir` with the task typed in. Returns (log, seconds, stopped),
    stopped being None, "timeout" or "runaway" (the log outgrew MAX_LOG_BYTES)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))
    env["GENESIS_WORKSPACE"] = str(workdir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["COLUMNS"] = "200"  # fewer wrapped lines for parse_log
    # Every trial starts with an empty memory, outside the trial's folder
    # (siblings are listable): the shared one carried a site task's
    # "preferences" into every later trial (2026-09-28 → 2026-09-30).
    memory = Path(tempfile.mkdtemp(prefix="genesis-bench-memory-"))
    env["GENESIS_MEMORY_DIR"] = str(memory)
    # Историята на чата — също временна (2026-10-11): всеки пуск записваше
    # сесия в истинската история на оператора, ~64 на един bench_all.
    history = Path(tempfile.mkdtemp(prefix="genesis-bench-history-"))
    env["GENESIS_HISTORY_DIR"] = str(history)
    log_path = workdir.parent / f"{workdir.name}.log"
    t0 = time.time()
    try:
        return _chat(genesis_cmd, task, workdir, timeout, env, log_path, t0)
    finally:
        shutil.rmtree(memory, ignore_errors=True)
        shutil.rmtree(history, ignore_errors=True)
        _drop_checkpoints(workdir)


def _drop_checkpoints(workdir: Path) -> None:
    """Снимките за /undo (GENESIS_HOME/checkpoints/<ключ на папката>) на папка
    за еднократна употреба: ~200 KB на пуск, никога не се чистеха (одит
    2026-10-11)."""
    try:
        from genesis_agent import checkpoints
        store = checkpoints._store(workdir.resolve())
    except Exception:  # чистенето не бива да чупи измерването
        return
    shutil.rmtree(store, ignore_errors=True)


def _chat(genesis_cmd: list[str], task: str, workdir: Path, timeout: int, env: dict,
          log_path: Path, t0: float) -> tuple[str, float, str | None]:
    with log_path.open("wb") as log:
        proc = subprocess.Popen(genesis_cmd, cwd=workdir, env=env, stdin=subprocess.PIPE,
                                stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=os.name != "nt")
        try:
            proc.stdin.write((task.strip() + "\nизход\n").encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass
        stopped = None
        while proc.poll() is None:
            if time.time() - t0 > timeout:
                stopped = "timeout"
            elif log_path.stat().st_size > MAX_LOG_BYTES:
                stopped = "runaway"
            if stopped:
                _kill_tree(proc)
                proc.wait(timeout=30)
                break
            time.sleep(1)
    with log_path.open("rb") as f:
        text = f.read(MAX_LOG_BYTES).decode("utf-8", "replace")
    return text, time.time() - t0, stopped


def run_hidden(test_python: str, project: Path, workdir: Path) -> tuple[int, int, str]:
    hidden = workdir.parent / f"{workdir.name}.hidden"
    hidden.mkdir(exist_ok=True)
    shutil.copy2(project / "test_hidden.py", hidden / "test_hidden.py")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(workdir)
    env["PYTHONUTF8"] = "1"
    rc, out = run_capped([test_python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                          "--rootdir", str(hidden), str(hidden / "test_hidden.py")],
                         cwd=hidden, env=env, timeout=HIDDEN_TIMEOUT)
    if rc is None:
        out += f"\n[bench] таймаут на скритите тестове след {HIDDEN_TIMEOUT} s — спрени\n"
    (hidden / "pytest.txt").write_text(out, encoding="utf-8")  # why a run failed
    if rc is None:
        return 0, max(parse_pytest(out)[1], 1), out
    passed, total = parse_pytest(out)
    return passed, total, out


def summarize(runs: list[dict]) -> dict[str, dict]:
    """Per project: fully passing runs, share of hidden tests passed, seconds, tokens.
    Пусковете без отговор от модел (`infra`) не влизат в runs/ok — броят им е
    отделно, за да не се четат като грешка на модела (2026-10-11)."""
    by: dict[str, list[dict]] = {}
    for r in runs:
        by.setdefault(r["project"], []).append(r)
    out = {}
    for name, every in sorted(by.items()):
        rs = [r for r in every if not r.get("infra")]
        n = len(rs) or 1
        out[name] = {
            "runs": len(rs),
            "ok": sum(1 for r in rs if r["passed"] == r["total"]),
            "tests": sum(r["passed"] / r["total"] for r in rs) / n,
            "seconds": sum(r["seconds"] for r in rs) / n,
            "tokens": sum(r["tokens"] for r in rs) / n,
            "infra": len(every) - len(rs),
        }
    return out


def write_results(out_dir: Path, runs: list[dict], *, complete: bool, **extra) -> Path:
    """results.json след ВСЕКИ пуск, както bench_fcc: спрян или паднал bench не
    пазеше нищо — файлът се пишеше само накрая (одит 2026-10-11). `complete`
    казва на bench_all дали пускането е стигнало до края."""
    path = out_dir / "results.json"
    path.write_text(json.dumps({"date": datetime.now().isoformat(timespec="seconds"),
                                "complete": complete, **extra,
                                "summary": summarize(runs), "runs": runs},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def occupied(out_dir: Path, names: list[str]) -> list[str]:
    """Какво от предишно пускане вече е в `out_dir`: results.json или папка на
    пуск. Повторно пускане в същата папка падаше с FileExistsError на първия
    проект, а bench_all отчиташе стария results.json като нов (одит 2026-10-11)."""
    found = ["results.json"] if (out_dir / "results.json").exists() else []
    return found + [n for n in names if (out_dir / n).exists()]


def check_compare(path: Path | None) -> tuple[Path | None, str]:
    """(разгънат път, грешка) за --compare — проверено ПРЕДИ пусканията.
    Грешен път (или неразгънато `~` в PowerShell 5.1) завършваше часове бенч
    с traceback и без таблица (одит 2026-10-11)."""
    if path is None:
        return None, ""
    path = path.expanduser()
    try:
        json.loads(path.read_text("utf-8"))["summary"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return path, f"--compare {path}: не става за сравнение ({type(e).__name__}: {e})"
    return path, ""


def format_table(summary: dict[str, dict], before: dict[str, dict] | None = None) -> str:
    rows = [f"{'Проект':<16} {'Верни':>7} {'Тестове':>8} {'сек':>7} {'токени':>9}"
            + ("   преди" if before else "")]
    for name, s in summary.items():
        if not s["runs"]:
            rows.append(f"{name:<16}   без отговор от модела ({s.get('infra', 0)} пуска) — не се брои")
            continue
        row = (f"{name:<16} {s['ok']:>3}/{s['runs']:<3} {s['tests']:>7.0%} "
               f"{s['seconds']:>7.0f} {s['tokens']:>9.0f}")
        if before:
            b = before.get(name)
            row += (f"   {b['ok']}/{b['runs']}, {b['tests']:.0%}, {b['seconds']:.0f} s"
                    if b else "   —")
        rows.append(row)
    runs = sum(s["runs"] for s in summary.values())
    ok = sum(s["ok"] for s in summary.values())
    infra = sum(s.get("infra", 0) for s in summary.values())
    rows.append(f"{'ОБЩО':<16} {ok:>3}/{runs:<3}"
                + (f"   + {infra} пуска без отговор от модела (не се броят)" if infra else ""))
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", help="comma-separated project names")
    ap.add_argument("--runs", type=int, default=1, help="runs per project (default 1)")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per run (default 900)")
    ap.add_argument("--out", type=Path, help="where trial folders and results.json go")
    ap.add_argument("--test-python", help="interpreter for the hidden tests (default: the "
                                          "one Genesis uses for the user's projects)")
    ap.add_argument("--compare", type=Path, help="an earlier results.json to show next to this run")
    args = ap.parse_args(argv)

    from genesis_agent.budget import LOG_PATH
    from genesis_agent.paths import ensure_utf8_streams, project_python

    ensure_utf8_streams()  # a piped Windows stdout is cp1251: "✅" would crash the report

    projects = sorted(p for p in PROJECTS_DIR.iterdir() if (p / "task.txt").is_file())
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        unknown = wanted - {p.name for p in projects}
        if unknown:
            print(f"Няма такива проекти: {', '.join(sorted(unknown))}")
            return 2
        projects = [p for p in projects if p.name in wanted]
    compare_path, err = check_compare(args.compare)
    if err:
        print(err)
        return 2

    # `~` не се разгъва от PowerShell 5.1 и cmd — стига до тук буквално (2026-10-11).
    out_dir = (args.out.expanduser() if args.out else
               Path(tempfile.gettempdir()) / "genesis-bench" / datetime.now().strftime("%Y%m%d-%H%M%S"))
    taken = occupied(out_dir, [f"{p.name}-{i}" for p in projects for i in range(1, args.runs + 1)])
    if taken:
        print(f"В {out_dir} вече има пускане ({', '.join(taken[:3])}{' …' if len(taken) > 3 else ''}) "
              f"— дай нова --out.")
        return 2
    test_python = os.path.expanduser(args.test_python) if args.test_python else project_python()
    # Без pytest всеки проект е „0/1 скрити“ без никакъв намек защо (одит
    # 2026-10-11) — проверява се веднъж, преди да се похарчи квота.
    if missing_modules(test_python, ["pytest"]):
        print(f"Python-ът за скритите тестове ({test_python}) няма pytest или не тръгва.\n"
              f"Инсталирай го: \"{test_python}\" -m pip install pytest — или дай --test-python.")
        return 2
    skipped: dict[str, list[str]] = {}
    for p in projects:
        lacks = missing_modules(test_python, NEEDS.get(p.name, ()))
        if lacks:
            skipped[p.name] = lacks
    out_dir.mkdir(parents=True, exist_ok=True)
    genesis_cmd = [sys.executable, "-m", "genesis_agent.cli"]
    print(f"Genesis: {' '.join(genesis_cmd)} (код от {REPO})")
    print(f"Скрити тестове с: {test_python}")
    print(f"Папки и логове: {out_dir}\n")
    for name, lacks in skipped.items():
        print(f"⏭  {name}: пропуснат — Python-ът за скритите тестове няма {', '.join(lacks)}")

    runs: list[dict] = []
    for project in projects:
        if project.name in skipped:
            continue
        task = (project / "task.txt").read_text("utf-8")
        for i in range(1, args.runs + 1):
            workdir = out_dir / f"{project.name}-{i}"
            workdir.mkdir()
            offset = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
            log, seconds, stopped = run_genesis(genesis_cmd, task, workdir, args.timeout)
            tokens, models = usage_since(_log_lines_since(LOG_PATH, offset))
            infra = is_infra(models, log)
            passed, total = (0, 1) if infra else run_hidden(test_python, project, workdir)[:2]
            run = {"project": project.name, "run": i, "passed": passed, "total": total,
                   "seconds": round(seconds, 1), "stopped": stopped, "tokens": tokens,
                   "models": models, "dropped": parse_log(log), "infra": infra}
            runs.append(run)
            write_results(out_dir, runs, complete=False, skipped=skipped)
            if infra:
                print(f"⚠️  {project.name} #{i}: моделът не отговори — не се брои "
                      f"(виж {workdir.name}.log)", flush=True)
                continue
            mark = "✅" if passed == total else "❌"
            extra = {"timeout": " (таймаут)", "runaway": " (зацикли — спрян)"}.get(stopped, "")
            used = ", ".join(f"{m} ×{n}" for m, n in models.items()) or "няма отговор"
            print(f"{mark} {project.name} #{i}: {passed}/{total} скрити, {seconds:.0f} s, "
                  f"{run['tokens']} токена{extra} — {used}", flush=True)

    summary = summarize(runs)
    write_results(out_dir, runs, complete=True, skipped=skipped)
    before, err = load_compare(compare_path)
    if err:
        print(err)
    print("\n" + format_table(summary, before))
    print(f"\nРезултати: {out_dir / 'results.json'}")
    infra_runs = sum(s["infra"] for s in summary.values())
    return 0 if not infra_runs and all(s["ok"] == s["runs"] for s in summary.values()) else 1


def load_compare(path: Path | None) -> tuple[dict | None, str]:
    """(summary, грешка) на по-ранен results.json; грешката не чупи отчета."""
    if path is None:
        return None, ""
    try:
        return json.loads(path.read_text("utf-8")).get("summary") or {}, ""
    except (OSError, ValueError, AttributeError) as e:
        return None, f"--compare {path}: {e}"


if __name__ == "__main__":
    sys.exit(main())
