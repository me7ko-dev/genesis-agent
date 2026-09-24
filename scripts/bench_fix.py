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
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genesis_agent import repo_agent
from genesis_agent.brain import Brain

TEST = "test_app.py"

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
}


class _Counting(Brain):
    """Brain that sums the usage of every call, so tokens per fix are real."""
    prompt = 0
    completion = 0
    pin: tuple[str, str] | None = None

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        if _Counting.pin:
            p, m = _Counting.pin
            self.chain = [{"provider": p, "model": m, "size_b": 0, "supports_tools": True}]
            self.local = None

    def complete(self, *a, **kw):
        reply = super().complete(*a, **kw)
        usage = getattr(reply, "usage", None) or {}
        _Counting.prompt += usage.get("prompt_tokens", 0) or 0
        _Counting.completion += usage.get("completion_tokens", 0) or 0
        return reply


def _run_test(root: Path) -> bool:
    r = subprocess.run([sys.executable, TEST], cwd=root, capture_output=True, text=True, timeout=60,
                       check=False)
    return r.returncode == 0 and "ALL OK" in r.stdout


def bench_one(name: str, task: str, files: dict[str, str], rounds: int) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"bench_fix_{name}_"))
    for rel, content in files.items():
        (root / rel).write_text(content, encoding="utf-8")
    assert not _run_test(root), f"{name}: the planted bug must make the test fail"
    test_before = (root / TEST).read_bytes()

    _Counting.prompt = _Counting.completion = 0
    t0 = time.time()
    try:
        out = repo_agent.repair(root, task, test_command=f'"{sys.executable}" {TEST}',
                                max_rounds=rounds, on_status=lambda msg: None)
        reported, used_rounds = out.success, out.rounds
    except Exception as e:  # a crash is a failed fix, recorded as such
        reported, used_rounds = False, 0
        print(f"    ! {type(e).__name__}: {e}"[:120])
    elapsed = time.time() - t0

    test_touched = (root / TEST).read_bytes() != test_before
    passed = _run_test(root) and not test_touched
    shutil.rmtree(root, ignore_errors=True)
    return {"name": name, "fixed": passed, "reported": reported, "rounds": used_rounds,
            "test_touched": test_touched, "sec": round(elapsed, 1),
            "prompt": _Counting.prompt, "completion": _Counting.completion}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default="", help="comma-separated project names")
    ap.add_argument("--model", default="", help="provider/model to pin (default: the configured chain)")
    ap.add_argument("--rounds", type=int, default=6)
    args = ap.parse_args()

    if args.model:
        p, _, m = args.model.partition("/")
        _Counting.pin = (p, m)
    repo_agent.Brain = _Counting  # repair() builds its Brain through this name

    names = [n for n in args.only.split(",") if n] or list(PROJECTS)
    results = []
    print(f"genesis fix — {len(names)} проекта, {args.model or 'конфигурираната верига'}\n")
    for name in names:
        task, files = PROJECTS[name]
        r = bench_one(name, task, files, args.rounds)
        results.append(r)
        flag = "✅" if r["fixed"] else ("⚠️ пипна теста" if r["test_touched"] else "❌")
        lie = "  (каза „поправено“, не е)" if r["reported"] and not r["fixed"] else ""
        print(f"  {flag:3} {name:16} рунда={r['rounds']:<2} {r['sec']:6.1f}s  "
              f"токени={r['prompt'] + r['completion']:>7}{lie}", flush=True)

    n = len(results)
    fixed = sum(r["fixed"] for r in results)
    toks = sum(r["prompt"] + r["completion"] for r in results)
    secs = sum(r["sec"] for r in results)
    false_claims = sum(1 for r in results if r["reported"] and not r["fixed"])
    print(f"\nПоправени: {fixed}/{n} ({round(100 * fixed / n)}%)  |  "
          f"средно {secs / n:.1f}s и {toks // n} токена на задача  |  "
          f"фалшиво „поправено“: {false_claims}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
