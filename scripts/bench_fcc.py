#!/usr/bin/env python3
"""
Benchmark on freeCodeCamp's daily Python challenges: Genesis gets the task
text and two examples, then every test of the challenge runs hidden.

    python scripts/bench_fcc.py --selfcheck            # the parser vs. fCC's own solutions
    python scripts/bench_fcc.py                        # 20 challenges spread over the list
    python scripts/bench_fcc.py --sample 40 --runs 2
    python scripts/bench_fcc.py --only 1,201 --compare old/results.json
    python scripts/bench_fcc.py --update               # pull the newest challenges first

Why: bench/projects/ has 10 hand-written projects; this adds hundreds of small
specs whose tests somebody else wrote. The challenge files are freeCodeCamp's
copyrighted curriculum, so they are never committed here: a sparse clone lives
in ~/.genesis/fcc and results.json records the commit it came from.

Each run is exactly bench_projects.py's (fresh folder, the chat with the task
on stdin, `изход`), except the answer must be `solution.py` in that folder.
Each hidden test then runs in its own process with the Python Genesis uses
for the user's projects: solution.py first, the test code after it, in one
namespace — as fCC's runPython does it.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import textwrap
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_projects as bp

FCC_REPO = "https://github.com/freeCodeCamp/freeCodeCamp.git"
FCC_DIR = Path.home() / ".genesis" / "fcc"
BLOCK = "curriculum/challenges/english/blocks/daily-coding-challenges-python"
TEST_TIMEOUT = 10

_RUN_PYTHON = re.compile(r"runPython\(\s*`((?:\\.|[^`\\])*)`\s*\)", re.DOTALL)
_JS_BLOCK = re.compile(r"```js\n(.*?)```", re.DOTALL)
_PY_BLOCK = re.compile(r"```py\n(.*?)```", re.DOTALL)
_JS_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
# What a hint's JS may be besides the runPython calls; anything else (assert on
# a returned value, helpers that inspect the source) is not something plain
# Python can replay, so the challenge is skipped rather than half-tested.
_PLAIN_TEST_SHAPES = {"({test:()=>{}})", "({test:()=>})", "({test:()=>{}});"}


def js_template(s: str) -> str:
    """The string a JS template literal evaluates to (no ${} — see parse)."""
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c != "\\" or i + 1 == len(s):
            out.append(c)
            i += 1
            continue
        n = s[i + 1]
        if n in _JS_ESCAPES:
            out.append(_JS_ESCAPES[n])
            i += 2
        elif n == "x":
            out.append(chr(int(s[i + 2:i + 4], 16)))
            i += 4
        elif n == "u" and s[i + 2:i + 3] == "{":
            end = s.index("}", i)
            out.append(chr(int(s[i + 3:end], 16)))
            i = end + 1
        elif n == "u":
            out.append(chr(int(s[i + 2:i + 6], 16)))
            i += 6
        elif n == "\n":
            i += 2
        else:
            out.append(n)
            i += 2
    return "".join(out)


def _section(text: str, name: str) -> str:
    m = re.search(rf"^# --{name}--\s*$(.*?)(?=^# --|\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def parse(text: str) -> dict:
    """One challenge file → description, seed, solution and the tests. A test
    that is not plain runPython code makes the whole challenge `unsupported`."""
    title = re.search(r'^title:\s*"?(.*?)"?\s*$', text, re.MULTILINE).group(1)
    num = int(re.search(r"(\d+)", title).group(1))
    hints = _section(text, "hints")
    tests, unsupported = [], None
    for m in _JS_BLOCK.finditer(hints):
        js = m.group(1)
        label = hints[:m.start()].rstrip().rsplit("\n\n", 1)[-1].strip()
        codes = _RUN_PYTHON.findall(js)
        rest = re.sub(r"\s|;", "", _RUN_PYTHON.sub("", js))
        if not codes or rest not in _PLAIN_TEST_SHAPES:
            unsupported = f"не е чист runPython: {label[:60]}"
        elif any("${" in c for c in codes):
            unsupported = f"${{}} в теста: {label[:60]}"
        tests.append({"label": label,
                      "code": "\n".join(textwrap.dedent(js_template(c)).strip() for c in codes)})
    seed = _PY_BLOCK.search(text.split("## --seed-contents--", 1)[-1])
    solution = _PY_BLOCK.search(_section(text, "solutions"))
    description = _section(text, "description")
    return {
        "num": num,
        "title": title,
        "description": description,
        "seed": seed.group(1).rstrip() + "\n" if seed else "",
        "solution": solution.group(1) if solution else "",
        "tests": tests,
        "unsupported": unsupported or (None if tests else "няма тестове"),
    }


def load(fcc_dir: Path = FCC_DIR) -> list[dict]:
    files = sorted((fcc_dir / BLOCK).glob("*.md"))
    return sorted((parse(f.read_text("utf-8")) for f in files), key=lambda c: c["num"])


def fetch(fcc_dir: Path, update: bool) -> str:
    """The sparse clone (one folder of the curriculum); returns its commit."""
    def git(*args: str, cwd: Path | None = None) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout.strip()
    if not (fcc_dir / ".git").is_dir():
        print(f"Свалям упражненията в {fcc_dir} …", flush=True)
        git("clone", "--depth", "1", "--filter=blob:none", "--sparse", FCC_REPO, str(fcc_dir))
        git("sparse-checkout", "set", BLOCK, cwd=fcc_dir)
    elif update:
        git("fetch", "--depth", "1", "origin", "HEAD", cwd=fcc_dir)
        git("reset", "--hard", "FETCH_HEAD", cwd=fcc_dir)
    return git("log", "-1", "--format=%h %cs", cwd=fcc_dir)


def pick(challenges: list[dict], sample: int) -> list[dict]:
    """`sample` challenges evenly over the list — the same ones every time, so
    two runs compare, and early and late (harder) challenges both appear."""
    if sample >= len(challenges):
        return challenges
    step = len(challenges) / sample
    return [challenges[int(i * step + step / 2)] for i in range(sample)]


def task_text(ch: dict, examples: int = 2) -> str:
    """No source and no title: with "freeCodeCamp, Challenge 10" in the task the
    first baseline run searched the web for the answer (and took the JS name)."""
    shown = "\n".join(f"- {t['label']}" for t in ch["tests"][:examples])
    return (f"Напиши решението на Python във файл `solution.py` в текущата папка, "
            f"със същото име и параметри като тук:\n\n"
            f"```python\n{ch['seed']}```\n\n"
            f"Условието (на английски):\n\n{ch['description']}\n\n"
            f"Примери:\n{shown}\n")


_RUNNER = r'''
import json, sys, os
sol, test = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.dirname(sol))
g = {"__name__": "solution"}
try:
    exec(compile(open(sol, encoding="utf-8").read(), sol, "exec"), g)
    exec(compile(open(test, encoding="utf-8").read(), "hidden_test", "exec"), g)
    r = {"ok": True}
except BaseException as e:
    r = {"ok": False, "error": (type(e).__name__ + ": " + str(e))[:300]}
print("\n@@RESULT@@" + json.dumps(r))
'''


def run_tests(python: str, solution: Path, tests: list[dict]) -> list[dict]:
    """Each test in its own process: an endless loop or sys.exit in one does
    not take the others down. The test files are gone afterwards — the next
    run's agent can list sibling folders (it did), so nothing hidden stays."""
    if not solution.is_file():
        return [{"ok": False, "error": "няма solution.py"} for _ in tests]
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "runner.py"
        runner.write_text(_RUNNER, encoding="utf-8")
        results = []
        for i, t in enumerate(tests):
            test_file = Path(tmp) / f"test_{i}.py"
            test_file.write_text(t["code"], encoding="utf-8")
            try:
                r = subprocess.run([python, str(runner), str(solution), str(test_file)],
                                   cwd=solution.parent, stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   timeout=TEST_TIMEOUT, check=False)
                tail = r.stdout.rsplit("@@RESULT@@", 1)
                res = json.loads(tail[1]) if len(tail) == 2 else {
                    "ok": False, "error": (r.stderr.strip().splitlines() or ["без изход"])[-1][:300]}
            except subprocess.TimeoutExpired:
                res = {"ok": False, "error": f"таймаут {TEST_TIMEOUT} s"}
            results.append(res)
    return results


_TOOL = re.compile(r"🔧 ([A-Z_]+) ")
_ROUND_CAP = "Достигнат таван"


def tool_counts(log: str) -> dict[str, int]:
    """Which tools a run used — WEB_SEARCH on a five-line function is a finding."""
    return dict(Counter(_TOOL.findall(log)))


def selfcheck(python: str, challenges: list[dict]) -> int:
    """fCC's own solution must pass every test the parser extracted, or the
    parser — not Genesis — would be what the numbers measure."""
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        for ch in challenges:
            sol = Path(tmp) / f"c{ch['num']}" / "solution.py"
            sol.parent.mkdir()
            sol.write_text(ch["solution"], encoding="utf-8")
            res = run_tests(python, sol, ch["tests"])
            failed = [(t["label"], r["error"]) for t, r in zip(ch["tests"], res) if not r["ok"]]
            if failed:
                bad += 1
                print(f"❌ {ch['title']}: {failed[0][0]} → {failed[0][1]}")
    print(f"Официалните решения: {len(challenges) - bad}/{len(challenges)} минават всички тестове")
    return 1 if bad else 0


def weak_spots(runs: list[dict]) -> str:
    lines = []
    for r in runs:
        if r["passed"] == r["total"]:
            continue
        why = r["failed"][0] if r["failed"] else {"label": "?", "error": "?"}
        extra = {"timeout": ", таймаут", "runaway": ", зацикли"}.get(r["stopped"], "")
        extra += ", таван на рундовете" if r.get("round_cap") else ""
        files = ", ".join(r["files"]) or "нищо"
        tools = " ".join(f"{t}×{n}" for t, n in sorted(r.get("tools", {}).items(), key=lambda x: -x[1]))
        lines.append(f"- {r['title']} #{r['run']}: {r['passed']}/{r['total']}{extra}; файлове: {files}\n"
                     f"    инструменти: {tools or 'няма'}\n"
                     f"    {why['label'][:110]}\n    → {why['error'][:160]}")
    return "\n".join(lines) or "няма — всичко минава"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sample", type=int, default=20, help="challenges spread over the list (default 20)")
    ap.add_argument("--only", help="comma-separated challenge numbers")
    ap.add_argument("--runs", type=int, default=1, help="runs per challenge (default 1)")
    ap.add_argument("--examples", type=int, default=2, help="tests shown in the task (default 2)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds per run (default 600)")
    ap.add_argument("--out", type=Path, help="where trial folders and results.json go")
    ap.add_argument("--fcc-dir", type=Path, default=FCC_DIR)
    ap.add_argument("--update", action="store_true", help="pull the newest challenges first")
    ap.add_argument("--selfcheck", action="store_true", help="run fCC's solutions, no Genesis")
    ap.add_argument("--test-python", help="interpreter for the hidden tests")
    ap.add_argument("--compare", type=Path, help="an earlier results.json to show next to this run")
    args = ap.parse_args(argv)

    from genesis_agent.budget import LOG_PATH
    from genesis_agent.paths import ensure_utf8_streams, project_python

    ensure_utf8_streams()
    commit = fetch(args.fcc_dir, args.update)
    everything = load(args.fcc_dir)
    usable = [c for c in everything if not c["unsupported"]]
    python = args.test_python or project_python()
    print(f"freeCodeCamp {commit}: {len(usable)} от {len(everything)} задачи стават за проверка")
    for c in everything:
        if c["unsupported"]:
            print(f"  пропусната {c['title']}: {c['unsupported']}")
    if args.selfcheck:
        return selfcheck(python, usable)

    if args.only:
        wanted = {int(n) for n in args.only.split(",") if n.strip()}
        chosen = [c for c in usable if c["num"] in wanted]
        missing = wanted - {c["num"] for c in chosen}
        if missing:
            print(f"Няма такива (или са пропуснати): {', '.join(map(str, sorted(missing)))}")
            return 2
    else:
        chosen = pick(usable, args.sample)

    out_dir = args.out or Path.home() / ".genesis" / "bench" / datetime.now().strftime("fcc-%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    genesis_cmd = [sys.executable, "-m", "genesis_agent.cli"]
    print(f"Genesis: {' '.join(genesis_cmd)} (код от {bp.REPO})")
    print(f"Скрити тестове с: {python}\nПапки и логове: {out_dir}\n")

    runs: list[dict] = []
    for ch in chosen:
        for i in range(1, args.runs + 1):
            workdir = out_dir / f"c{ch['num']:03d}-{i}"
            workdir.mkdir()
            offset = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
            log, seconds, stopped = bp.run_genesis(genesis_cmd, task_text(ch, args.examples),
                                                   workdir, args.timeout)
            res = run_tests(python, workdir / "solution.py", ch["tests"])
            tokens, models = bp.usage_since(bp._log_lines_since(LOG_PATH, offset))
            failed = [{"label": t["label"], "error": r["error"]}
                      for t, r in zip(ch["tests"], res) if not r["ok"]]
            run = {"project": f"c{ch['num']:03d}", "title": ch["title"], "run": i,
                   "passed": len(res) - len(failed), "total": len(res), "failed": failed,
                   "files": sorted(p.name for p in workdir.iterdir() if p.is_file()),
                   "seconds": round(seconds, 1), "stopped": stopped, "tokens": tokens,
                   "models": models, "dropped": bp.parse_log(log), "tools": tool_counts(log),
                   "round_cap": _ROUND_CAP in log}
            runs.append(run)
            mark = "✅" if not failed else "❌"
            tools_s = " ".join(f"{t}×{n}" for t, n in sorted(run["tools"].items(), key=lambda x: -x[1]))
            print(f"{mark} {ch['title']} #{i}: {run['passed']}/{run['total']} скрити, {seconds:.0f} s, "
                  f"{tokens} токена; {tools_s or 'без инструменти'}", flush=True)
            # after every run: a stopped bench keeps what it measured
            (out_dir / "results.json").write_text(
                json.dumps({"date": datetime.now().isoformat(timespec="seconds"), "fcc": commit,
                            "examples": args.examples, "summary": bp.summarize(runs), "runs": runs},
                           ensure_ascii=False, indent=1), encoding="utf-8")

    summary = bp.summarize(runs)
    before = json.loads(args.compare.read_text("utf-8")).get("summary") if args.compare else None
    print("\n" + bp.format_table(summary, before))
    print("\nСлаби места:\n" + weak_spots(runs))
    print(f"\nРезултати: {out_dir / 'results.json'}")
    return 0 if all(s["ok"] == s["runs"] for s in summary.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
