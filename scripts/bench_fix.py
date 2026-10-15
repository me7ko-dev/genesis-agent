#!/usr/bin/env python3
"""
Benchmark for `genesis fix`: real bugs in small projects, judged by their tests.

    python scripts/bench_fix.py                      # the configured chain, all projects
    python scripts/bench_fix.py --only median,cache  # a subset
    python scripts/bench_fix.py --model groq/openai/gpt-oss-120b   # one model pinned

Why this exists (NEXT_STEPS.md, 2026-09-23): `benchmark.py` measures writing a
small function from scratch. `genesis fix` does something else — it reads code
somebody else wrote, finds the bug and changes only that — and nothing measured
it. Without a number, "more accurate" is a guess.

Each project is a few files with ONE planted bug and a plain-Python test that
fails because of it. The task is phrased the way the operator writes: short,
sometimes Bulgarian, sometimes transliterated. A run counts as fixed only if:
  • the project's test passes when re-run HERE, independently of what the
    agent reported, and
  • the test file is byte-for-byte unchanged — "fixing" a bug by editing its
    test is a failure, not a success.

Reports success rate, rounds, prompt/completion tokens and wall time per
project, and totals. Runs against the real providers: it spends free quota.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_projects as bp

from genesis_agent.paths import ensure_utf8_streams, project_python

# Тежките модули (repo_agent, brain, config) се внасят чак в main(), СЛЕД
# _isolate_memory(): config чете GENESIS_MEMORY_DIR при първото си внасяне.

TEST = "test_app.py"
TEST_TIMEOUT = 60

# Projects whose test needs a package outside the standard library, and which
# `genesis fix` must therefore test with the PROJECT'S Python, found on its own
# (no test command passed in): the regression for PR #30, where pytest ran in
# the pipx venv and every test died on `No module named 'reportlab'`. A machine
# without the packages (pytest included: the detected command is pytest) skips
# the project instead of counting a failure.
NEEDS: dict[str, tuple[str, ...]] = {"excel_total": ("openpyxl", "pytest")}

# name -> (task as the operator would write it, {file: content})
PROJECTS: dict[str, tuple[str, dict[str, str]]] = {
    "median": (
        "median() връща грешно при четен брой елементи",
        {
            "stats.py": "def median(xs):\n    s = sorted(xs)\n    return s[len(s) // 2]\n",
            TEST: "from stats import median\nassert median([3, 1, 2]) == 2\n"
                  "assert median([4, 1, 3, 2]) == 2.5\nprint('ALL OK')\n",
        },
    ),
    "pagination": (
        "paginate skips the first item of every page after the first",
        {
            "pages.py": "def paginate(items, page, size):\n"
                        "    start = page * size + 1 if page else 0\n"
                        "    return items[start:start + size]\n",
            TEST: "from pages import paginate\nxs = list(range(10))\n"
                  "assert paginate(xs, 0, 3) == [0, 1, 2]\nassert paginate(xs, 1, 3) == [3, 4, 5]\n"
                  "assert paginate(xs, 3, 3) == [9]\nprint('ALL OK')\n",
        },
    ),
    "mutable_default": (
        "add_tag трупа таговете между отделни извиквания",
        {
            "tags.py": "def add_tag(tag, tags=[]):\n    tags.append(tag)\n    return tags\n",
            TEST: "from tags import add_tag\nassert add_tag('a') == ['a']\nassert add_tag('b') == ['b']\n"
                  "assert add_tag('c', ['x']) == ['x', 'c']\nprint('ALL OK')\n",
        },
    ),
    "average": (
        "average([1, 2]) returns 1 instead of 1.5",
        {
            "mathx.py": "def average(xs):\n    if not xs:\n        return 0.0\n    return sum(xs) // len(xs)\n",
            TEST: "from mathx import average\nassert average([1, 2]) == 1.5\nassert average([]) == 0.0\n"
                  "assert average([2, 2, 2]) == 2\nprint('ALL OK')\n",
        },
    ),
    "search_case": (
        "tarsaneto ne namira nishto ako pisha s glavni bukvi",
        {
            "search.py": "def find(names, query):\n"
                         "    return [n for n in names if query in n.lower()]\n",
            TEST: "from search import find\nnames = ['Ivan', 'Maria', 'ivo']\n"
                  "assert find(names, 'iv') == ['Ivan', 'ivo']\nassert find(names, 'IV') == ['Ivan', 'ivo']\n"
                  "assert find(names, 'MAR') == ['Maria']\nprint('ALL OK')\n",
        },
    ),
    "config_key": (
        "load_config гърми с KeyError за timeout",
        {
            "config.py": "DEFAULTS = {'host': 'localhost', 'timeout': 30}\n\n"
                         "def load_config(overrides):\n"
                         "    cfg = dict(DEFAULTS)\n    cfg.update(overrides)\n"
                         "    return {'host': cfg['host'], 'timeout': cfg['timout']}\n",
            TEST: "from config import load_config\nassert load_config({}) == {'host': 'localhost', 'timeout': 30}\n"
                  "assert load_config({'timeout': 5})['timeout'] == 5\nprint('ALL OK')\n",
        },
    ),
    "cache": (
        "after update_price, get_price still returns the old price",
        {
            "store.py": "from cache import Cache\n\n_prices = {'apple': 1.0}\n_cache = Cache()\n\n"
                        "def get_price(item):\n    hit = _cache.get(item)\n    if hit is not None:\n"
                        "        return hit\n    _cache.set(item, _prices[item])\n    return _prices[item]\n\n"
                        "def update_price(item, price):\n    _prices[item] = price\n",
            "cache.py": "class Cache:\n    def __init__(self):\n        self._d = {}\n\n"
                        "    def get(self, k):\n        return self._d.get(k)\n\n"
                        "    def set(self, k, v):\n        self._d[k] = v\n\n"
                        "    def delete(self, k):\n        self._d.pop(k, None)\n",
            TEST: "from store import get_price, update_price\nassert get_price('apple') == 1.0\n"
                  "update_price('apple', 2.5)\nassert get_price('apple') == 2.5\nprint('ALL OK')\n",
        },
    ),
    "factorial": (
        "factorial(0) влиза в безкрайна рекурсия",
        {
            "fact.py": "def factorial(n):\n    if n == 1:\n        return 1\n    return n * factorial(n - 1)\n",
            TEST: "import sys\nsys.setrecursionlimit(200)\nfrom fact import factorial\n"
                  "assert factorial(0) == 1\nassert factorial(1) == 1\nassert factorial(5) == 120\nprint('ALL OK')\n",
        },
    ),
    "sort_desc": (
        "top_scores should return the highest scores first",
        {
            "scores.py": "def top_scores(scores, n):\n    return sorted(scores)[:n]\n",
            TEST: "from scores import top_scores\nassert top_scores([5, 1, 9, 3], 2) == [9, 5]\n"
                  "assert top_scores([1], 3) == [1]\nprint('ALL OK')\n",
        },
    ),
    "strip_input": (
        "check_password отказва правилната парола, ако идва от файл",
        {
            "auth.py": "import hmac\n\nSECRET = 'hunter2'\n\n"
                       "def check_password(given):\n    return hmac.compare_digest(given, SECRET)\n",
            TEST: "from auth import check_password\nassert check_password('hunter2')\n"
                  "assert check_password('hunter2\\n')\nassert not check_password('hunter3')\nprint('ALL OK')\n",
        },
    ),
    "renamed_import": (
        "app.py crashes on import after the refactor",
        {
            "textutil.py": "def slugify_text(s):\n    return '-'.join(s.lower().split())\n",
            "app.py": "from textutil import slugify\n\ndef make_url(title):\n    return '/posts/' + slugify(title)\n",
            TEST: "from app import make_url\nassert make_url('Hello World') == '/posts/hello-world'\nprint('ALL OK')\n",
        },
    ),
    "date_parse": (
        "parse_date не приема дати като 2026-09-24",
        {
            "dates.py": "from datetime import datetime\n\ndef parse_date(s):\n"
                        "    return datetime.strptime(s, '%d.%m.%Y').date()\n",
            TEST: "from datetime import date\nfrom dates import parse_date\n"
                  "assert parse_date('2026-09-24') == date(2026, 9, 24)\nprint('ALL OK')\n",
        },
    ),
    "excel_total": (
        "в Excel отчета редът ОБЩО показва грешна сума",
        {
            "pyproject.toml": "[project]\nname = 'shop-report'\nversion = '0.1'\ndependencies = ['openpyxl']\n",
            "report.py": "from openpyxl import Workbook\n\n\ndef write_report(rows, path):\n"
                         "    wb = Workbook()\n    ws = wb.active\n"
                         "    ws.append(['Продукт', 'Количество', 'Цена', 'Сума'])\n"
                         "    for name, qty, price in rows:\n"
                         "        ws.append([name, qty, price, round(qty * price, 2)])\n"
                         "    ws.append(['ОБЩО', None, None, round(sum(p for _, _, p in rows), 2)])\n"
                         "    wb.save(path)\n",
            TEST: "import os\nimport tempfile\n\nfrom openpyxl import load_workbook\n\n"
                  "from report import write_report\n\n\ndef test_total():\n"
                  "    path = os.path.join(tempfile.mkdtemp(), 'r.xlsx')\n"
                  "    write_report([('Хляб', 3, 1.8), ('Мляко', 2, 2.5)], path)\n"
                  "    ws = load_workbook(path).active\n"
                  "    last = [c.value for c in ws[ws.max_row]]\n"
                  "    assert last[0] == 'ОБЩО'\n    assert last[3] == 10.4\n\n\n"
                  "if __name__ == '__main__':\n    test_total()\n    print('ALL OK')\n",
        },
    ),
}


# Броячи за текущия проект: токени по usage, успешни извиквания на модела и
# грешки на веригата. `calls == 0` значи, че моделът изобщо не отговори (няма
# ключове, мрежа) — това не е неуспешна поправка (одит 2026-10-11).
USAGE = {"prompt": 0, "completion": 0, "calls": 0, "errors": 0}


def _counting_brain(brain_cls: type) -> type:
    """Brain, който брои usage на всяко извикване — токените на поправка са истински."""

    class _Counting(brain_cls):  # type: ignore[valid-type, misc]
        pin: tuple[str, str] | None = None

        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            if _Counting.pin:
                p, m = _Counting.pin
                self.chain = [{"provider": p, "model": m, "size_b": 0, "supports_tools": True}]
                self.local = None

        def _error_result(self, msg: str):
            USAGE["errors"] += 1
            return super()._error_result(msg)

        def complete(self, *a, **kw):
            errors = USAGE["errors"]
            reply = super().complete(*a, **kw)
            if USAGE["errors"] == errors:
                USAGE["calls"] += 1
            usage = getattr(reply, "usage", None) or {}
            USAGE["prompt"] += usage.get("prompt_tokens", 0) or 0
            USAGE["completion"] += usage.get("completion_tokens", 0) or 0
            return reply

    return _Counting


def _isolate_memory() -> Path | None:
    """Временна памет за repair() в ТОЗИ процес, преди genesis_agent.config да
    се е внесъл: без нея всяко извикване на инструмент ставаше епизод в
    episodes.db на чекаута (одит 2026-10-11). bench_projects прави същото за
    всеки пуск. None, ако операторът вече е задал своя GENESIS_MEMORY_DIR."""
    if os.environ.get("GENESIS_MEMORY_DIR"):
        return None
    memory = Path(tempfile.mkdtemp(prefix="genesis-bench-fix-memory-"))
    os.environ["GENESIS_MEMORY_DIR"] = str(memory)
    return memory


def _load_agent(model: str):
    """repair() с броещия Brain и пясъчник, който отказва всяко CONFIRM.

    Под bench_all stdin е конзолата (isatty → „interactive“), а stdout е
    fix.log: CONFIRM (pip install, rm -rf __pycache__) питаше във файла и
    чакаше на конзолата завинаги, без видим въпрос (одит 2026-10-11). Мярката
    не бива да зависи от това дали някой седи пред нея — както чатът в
    bench_projects, който получава тръба и отказва."""
    from genesis_agent import repo_agent, sandbox
    from genesis_agent.brain import Brain

    sandbox.set_policy(sandbox.SandboxPolicy(mode="deny"))
    counting = _counting_brain(Brain)
    if model:
        p, _, m = model.partition("/")
        counting.pin = (p, m)
    repo_agent.Brain = counting  # repair() builds its Brain through this name
    return repo_agent.repair


def _run_test(root: Path) -> bool:
    """The independent re-check, with the interpreter `genesis fix` itself
    uses for the project — under pipx, `sys.executable` has none of its packages.
    С таван и убиване на цялото дърво: „поправка“ с безкраен цикъл чупеше целия
    bench след 11 минути и губеше вече измереното (одит 2026-10-11)."""
    rc, out = bp.run_capped([project_python(root), TEST], cwd=root, env=None, timeout=TEST_TIMEOUT)
    return rc == 0 and "ALL OK" in out


def _missing(root: Path, packages: tuple[str, ...]) -> list[str]:
    return [p for p in packages if not _imports(root, p)]


def _imports(root: Path, package: str) -> bool:
    rc, _ = bp.run_capped([project_python(root), "-c", f"import {package}"], cwd=root, env=None,
                          timeout=TEST_TIMEOUT)
    return rc == 0


def bench_one(name: str, task: str, files: dict[str, str], rounds: int, repair) -> dict | None:
    """One project. None when the machine lacks a package the project needs."""
    root = Path(tempfile.mkdtemp(prefix=f"bench_fix_{name}_"))
    for rel, content in files.items():
        (root / rel).write_text(content, encoding="utf-8")
    needs = NEEDS.get(name, ())
    if _missing(root, needs):
        shutil.rmtree(root, ignore_errors=True)
        return None
    assert not _run_test(root), f"{name}: the planted bug must make the test fail"
    test_before = (root / TEST).read_bytes()
    # A project with a package outside the stdlib is tested the way `genesis
    # fix` finds it (pyproject.toml → pytest with the project's Python); the
    # rest get the plain-script command, with that same interpreter.
    test_command = None if needs else f'"{project_python(root)}" {TEST}'

    USAGE.update(prompt=0, completion=0, calls=0, errors=0)
    log_path, offset = _budget_offset()
    t0 = time.time()
    try:
        out = repair(root, task, test_command=test_command,
                     max_rounds=rounds, on_status=lambda msg: None)
        reported, used_rounds = out.success, out.rounds
    except Exception as e:  # noqa: BLE001 — сривът е неуспешна поправка и се печата
        reported, used_rounds = False, 0
        print(f"    ! {type(e).__name__}: {e}"[:120])
    elapsed = time.time() - t0

    test_touched = (root / TEST).read_bytes() != test_before
    passed = _run_test(root) and not test_touched
    shutil.rmtree(root, ignore_errors=True)
    return {"name": name, "fixed": passed, "reported": reported, "rounds": used_rounds,
            "test_touched": test_touched, "sec": round(elapsed, 1),
            "prompt": USAGE["prompt"], "completion": USAGE["completion"],
            "infra": USAGE["calls"] == 0 and not _billed_since(log_path, offset)}


def _budget_offset() -> tuple[Path | None, int]:
    """Докъде е бюджетният лог преди проекта — за _billed_since."""
    try:
        from genesis_agent import budget
        path = Path(budget.LOG_PATH)
    except Exception:  # noqa: BLE001 — без лога остава само броячът на успешните извиквания
        return None, 0
    return path, path.stat().st_size if path.exists() else 0


def _billed_since(log_path: Path | None, offset: int) -> bool:
    """Има ли таксуван отговор от модел след `offset` — същото определение за
    infra като в bench_projects. Празен отговор е грешка на Brain (calls == 0),
    но моделът е отговорил и е платен: броеше се за „моделът не отговори“ и
    проектът изпадаше от сравнението в bench_all, докато bench_projects го
    брои за грешен пуск (измерено 2026-10-11, фалшив доставчик с празно
    съдържание: fix — infra, projects — 0/1)."""
    if log_path is None:
        return False
    return bool(bp.usage_since(bp._log_lines_since(log_path, offset))[1])


def _line(r: dict) -> str:
    """Ред от отчета. Знакът е сам в колоната, бележките — накрая: „⚠️ пипна
    теста“ беше по-широко от {flag:3} и разместваше реда (2026-10-11)."""
    if r.get("infra"):
        return f"  ⚠️  {r['name']:16} моделът не отговори — не се брои"
    flag = "✅" if r["fixed"] else ("⚠️" if r["test_touched"] else "❌")
    notes = (["пипна теста"] if r["test_touched"] else []) + \
            (["каза „поправено“, не е"] if r["reported"] and not r["fixed"] else [])
    return (f"  {flag}  {r['name']:16} рунда={r['rounds']:<2} {r['sec']:6.1f}s  "
            f"токени={r['prompt'] + r['completion']:>7}" + (f"  ({'; '.join(notes)})" if notes else ""))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated project names")
    ap.add_argument("--model", default="", help="provider/model to pin (default: the configured chain)")
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--out", type=Path, help="папка за results.json (същият формат като bench_projects)")
    args = ap.parse_args(argv)

    # Пренасочен или пуснат през тръба stdout на български Windows е cp1251:
    # първото „✅“ сриваше bench-а СЛЕД като вече е похарчил извиквания (2026-10-11).
    ensure_utf8_streams()
    # Имената се изчистват и проверяват ПРЕДИ първия проект: „median, pagination“
    # падаше с KeyError след вече пуснатите (одит 2026-10-11).
    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(PROJECTS)
    unknown = [n for n in names if n not in PROJECTS]
    if unknown:
        print(f"Няма такива проекти: {', '.join(unknown)} (има: {', '.join(PROJECTS)})")
        return 2
    out = args.out.expanduser() if args.out else None

    memory = _isolate_memory()
    try:
        return _run(names, args.rounds, args.model, out, _load_agent(args.model))
    finally:
        if memory is not None:
            os.environ.pop("GENESIS_MEMORY_DIR", None)
            shutil.rmtree(memory, ignore_errors=True)


def _run(names: list[str], rounds: int, model: str, out: Path | None, repair) -> int:
    results: list[dict] = []
    print(f"genesis fix — {len(names)} проекта, {model or 'конфигурираната верига'}\n")
    for name in names:
        task, files = PROJECTS[name]
        r = bench_one(name, task, files, rounds, repair)
        if r is None:
            print(f"  ⏭  {name:16} пропуснат: Python-ът на проекта ({project_python()}) няма "
                  f"{', '.join(NEEDS[name])}", flush=True)
            continue
        results.append(r)
        print(_line(r), flush=True)
        if out is not None:  # след всеки проект: спрян bench пази измереното
            write_results(out, results, model, complete=False, quiet=True)

    counted = [r for r in results if not r.get("infra")]
    infra = len(results) - len(counted)
    if out is not None and results:
        write_results(out, results, model)
    n = len(counted)
    if not n:
        print("\nНито един проект не е пуснат" + (f" — моделът не отговори ({infra})." if infra else "."))
        return 1
    fixed = sum(r["fixed"] for r in counted)
    toks = sum(r["prompt"] + r["completion"] for r in counted)
    secs = sum(r["sec"] for r in counted)
    false_claims = sum(1 for r in counted if r["reported"] and not r["fixed"])
    print(f"\nПоправени: {fixed}/{n} ({round(100 * fixed / n)}%)  |  "
          f"средно {secs / n:.1f}s и {toks // n} токена на задача  |  "
          f"фалшиво „поправено“: {false_claims}"
          + (f"  |  без отговор от модела: {infra}" if infra else ""))
    # Код 1 при неуспех, както bench_projects — самостоятелно пускане иначе
    # винаги казваше „успех“ (2026-10-11).
    return 0 if fixed == n and not infra else 1


def summarize(results: list[dict]) -> dict[str, dict]:
    """Като bench_projects.summarize — за да ги сравнява scripts/bench_all.py.
    Проект без отговор от модела е runs=0, infra=1: не е неуспешна поправка."""
    out = {}
    for r in results:
        infra = bool(r.get("infra"))
        out[r["name"]] = {"runs": 0 if infra else 1, "ok": int(r["fixed"] and not infra),
                          "tests": float(r["fixed"] and not infra),
                          "seconds": r["sec"], "tokens": r["prompt"] + r["completion"],
                          "false_claim": bool(r["reported"] and not r["fixed"] and not infra),
                          "test_touched": r["test_touched"], "infra": int(infra)}
    return out


def write_results(out: Path, results: list[dict], model: str, *, complete: bool = True,
                  quiet: bool = False) -> Path:
    import json
    from datetime import datetime
    out.mkdir(parents=True, exist_ok=True)
    path = out / "results.json"
    path.write_text(json.dumps({"date": datetime.now().isoformat(timespec="seconds"),
                                "model": model or "chain", "complete": complete,
                                "summary": summarize(results), "runs": results},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    if not quiet:
        print(f"Резултати: {path}")
    return path


if __name__ == "__main__":
    raise SystemExit(main())
