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

_DROPPED = re.compile(r"\[Brain\] ✗ (\S+) след [\d.]+s: (\S+?)[:\s]")
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?)\b")
# A chat stuck printing the same error fills the disk long before the timeout:
# measured 2026-09-26, 1.2M lines in 5 minutes. A real run logs well under 1 MB.
MAX_LOG_BYTES = 20_000_000


def parse_log(text: str) -> dict[str, int]:
    """Why models dropped out of the chain (the `✗` lines), by reason."""
    return dict(Counter(reason for _model, reason in _DROPPED.findall(text)))


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


def run_genesis(genesis_cmd: list[str], task: str, workdir: Path, timeout: int) -> tuple[str, float, str | None]:
    """Chat in `workdir` with the task typed in. Returns (log, seconds, stopped),
    stopped being None, "timeout" or "runaway" (the log outgrew MAX_LOG_BYTES)."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))
    env["GENESIS_WORKSPACE"] = str(workdir)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["COLUMNS"] = "200"  # fewer wrapped lines for parse_log
    log_path = workdir.parent / f"{workdir.name}.log"
    t0 = time.time()
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
    r = subprocess.run([test_python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "--rootdir", str(hidden), str(hidden / "test_hidden.py")],
                       cwd=hidden, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300, check=False)
    out = r.stdout + r.stderr
    (hidden / "pytest.txt").write_text(out, encoding="utf-8")  # why a run failed
    passed, total = parse_pytest(out)
    return passed, total, out


def summarize(runs: list[dict]) -> dict[str, dict]:
    """Per project: fully passing runs, share of hidden tests passed, seconds, tokens."""
    by: dict[str, list[dict]] = {}
    for r in runs:
        by.setdefault(r["project"], []).append(r)
    out = {}
    for name, rs in sorted(by.items()):
        out[name] = {
            "runs": len(rs),
            "ok": sum(1 for r in rs if r["passed"] == r["total"]),
            "tests": sum(r["passed"] / r["total"] for r in rs) / len(rs),
            "seconds": sum(r["seconds"] for r in rs) / len(rs),
            "tokens": sum(r["tokens"] for r in rs) / len(rs),
        }
    return out


def format_table(summary: dict[str, dict], before: dict[str, dict] | None = None) -> str:
    rows = [f"{'Проект':<16} {'Верни':>7} {'Тестове':>8} {'сек':>7} {'токени':>9}"
            + ("   преди" if before else "")]
    for name, s in summary.items():
        row = (f"{name:<16} {s['ok']:>3}/{s['runs']:<3} {s['tests']:>7.0%} "
               f"{s['seconds']:>7.0f} {s['tokens']:>9.0f}")
        if before:
            b = before.get(name)
            row += (f"   {b['ok']}/{b['runs']}, {b['tests']:.0%}, {b['seconds']:.0f} s"
                    if b else "   —")
        rows.append(row)
    runs = sum(s["runs"] for s in summary.values())
    ok = sum(s["ok"] for s in summary.values())
    rows.append(f"{'ОБЩО':<16} {ok:>3}/{runs:<3}")
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

    out_dir = args.out or Path(tempfile.gettempdir()) / "genesis-bench" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    test_python = args.test_python or project_python()
    genesis_cmd = [sys.executable, "-m", "genesis_agent.cli"]
    print(f"Genesis: {' '.join(genesis_cmd)} (код от {REPO})")
    print(f"Скрити тестове с: {test_python}")
    print(f"Папки и логове: {out_dir}\n")

    runs: list[dict] = []
    for project in projects:
        task = (project / "task.txt").read_text("utf-8")
        for i in range(1, args.runs + 1):
            workdir = out_dir / f"{project.name}-{i}"
            workdir.mkdir()
            offset = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
            log, seconds, stopped = run_genesis(genesis_cmd, task, workdir, args.timeout)
            passed, total, _ = run_hidden(test_python, project, workdir)
            tokens, models = usage_since(_log_lines_since(LOG_PATH, offset))
            run = {"project": project.name, "run": i, "passed": passed, "total": total,
                   "seconds": round(seconds, 1), "stopped": stopped, "tokens": tokens,
                   "models": models, "dropped": parse_log(log)}
            runs.append(run)
            mark = "✅" if passed == total else "❌"
            extra = {"timeout": " (таймаут)", "runaway": " (зацикли — спрян)"}.get(stopped, "")
            models = ", ".join(f"{m} ×{n}" for m, n in models.items()) or "няма отговор"
            print(f"{mark} {project.name} #{i}: {passed}/{total} скрити, {seconds:.0f} s, "
                  f"{run['tokens']} токена{extra} — {models}", flush=True)

    summary = summarize(runs)
    (out_dir / "results.json").write_text(
        json.dumps({"date": datetime.now().isoformat(timespec="seconds"),
                    "summary": summary, "runs": runs}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    before = None
    if args.compare:
        before = json.loads(args.compare.read_text("utf-8")).get("summary")
    print("\n" + format_table(summary, before))
    print(f"\nРезултати: {out_dir / 'results.json'}")
    return 0 if all(s["ok"] == s["runs"] for s in summary.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
