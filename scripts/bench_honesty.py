"""Честността на хода, преди и след: сценариите от tests/test_honesty_bench.py.

    python scripts/bench_honesty.py              # този checkout
    python scripts/bench_honesty.py --ref main   # и main (отделно работно копие)

Без ключове и без мрежа: фалшив модел през истинския цикъл (`run_turn`,
истински RUN_CMD и pytest). „Хванати“ — колко от отговорите, които твърдят
неподкрепено, цикълът връща със забележка; „фалшиви тревоги“ — колко честни
отчета/обяснения/поискани промени връща напразно. Състоянието на оператора не се
пипа: тестовете минават през conftest.py на съответното копие.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "test_honesty_bench.py"


def run(tree: Path) -> list[dict]:
    """Сценариите в `tree` (неговият код, неговият conftest) → редовете JSON."""
    with tempfile.TemporaryDirectory(prefix="genesis-honesty-") as tmp:
        out = Path(tmp) / "out.jsonl"
        env = {**os.environ, "GENESIS_HONESTY_OUT": str(out), "PYTHONPATH": str(tree)}
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "tests/test_honesty_bench.py"], cwd=tree, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if not out.exists():
            sys.exit(f"Сценариите не тръгнаха в {tree} (pytest -q tests/test_honesty_bench.py там).")
        return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line]


def run_ref(ref: str) -> list[dict]:
    """Същите сценарии върху `ref`: временно работно копие + този файл със сценариите."""
    tmp = Path(tempfile.mkdtemp(prefix="genesis-honesty-wt-"))
    tree = tmp / "tree"
    try:
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "-q", "--detach", str(tree), ref],
                       check=True)
        shutil.copy2(SCENARIOS, tree / "tests" / SCENARIOS.name)
        return run(tree)
    finally:
        subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(tree)],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(tmp, ignore_errors=True)


def score(rows: list[dict]) -> str:
    push = [r for r in rows if r["push"]]
    calm = [r for r in rows if not r["push"]]
    caught = sum(r["pushed"] for r in push)
    false = sum(r["pushed"] for r in calm)
    return f"хванати {caught}/{len(push)}, фалшиви тревоги {false}/{len(calm)}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", help="и този комит/клон за сравнение (напр. main)")
    args = ap.parse_args()
    here = run(ROOT)
    other = run_ref(args.ref) if args.ref else None
    by_id = {r["id"]: r for r in other or []}
    head = f"| сценарий | трябва | {args.ref} | този |" if other else "| сценарий | трябва | този |"
    print(head)
    print("|" + "---|" * (4 if other else 3))
    mark = {True: "забележка", False: "—"}
    for r in here:
        cells = [r["id"], "забележка" if r["push"] else "без"]
        if other:
            o = by_id.get(r["id"])
            cells.append(("✅ " if o["pushed"] == o["push"] else "❌ ") + mark[o["pushed"]] if o else "?")
        cells.append(("✅ " if r["pushed"] == r["push"] else "❌ ") + mark[r["pushed"]])
        print("| " + " | ".join(cells) + " |")
    print()
    if other:
        print(f"{args.ref}: {score(other)}")
    print(f"този checkout: {score(here)}")
    return 0 if all(r["pushed"] == r["push"] for r in here) else 1


if __name__ == "__main__":
    raise SystemExit(main())
