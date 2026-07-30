#!/usr/bin/env python3
"""
Measure individual models, so the chain order in config.yaml is a measurement
rather than a guess.

    python3 scripts/bench_models.py                       # the configured coding chain
    python3 scripts/bench_models.py --tasks 10            # bigger sample
    python3 scripts/bench_models.py openrouter/some:free  # specific models

Each candidate is pinned as the ONLY entry in its chain, with the local model
off. Without that, a model that fails silently hands the task to the next one
and the score you write down belongs to the chain, not to the model.

Sample size is a real trade-off, not laziness: OpenRouter's free tier is around
50 requests a day, so a thorough benchmark spends the quota you were saving for
actual work. Six tasks reliably separates "broken" from "working" and does not
pretend to separate second place from third — when scores tie, decide on
something else and say so.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genesis_agent.benchmark import _SYS, TASKS
from genesis_agent.brain import Brain, _load_coding_chain
from genesis_agent.executor import run_python_subprocess

# The first 15 tasks score ~100% on everything and separate nothing; the hard
# set (edge cases, concurrency, parsing) is what discriminates.
HARD = TASKS[15:]


def pinned_brain(provider: str, model: str) -> Brain:
    brain = Brain(use_local=False)
    brain.chain = [{"provider": provider, "model": model,
                    "size_b": 0, "supports_tools": True}]
    brain.local = None
    return brain


def bench_one(provider: str, model: str, tasks: list) -> dict:
    passed, notes, t0 = 0, [], time.time()
    print(f"\n=== {provider}/{model} ===", flush=True)
    for i, (goal, check) in enumerate(tasks, 1):
        try:
            reply = pinned_brain(provider, model).complete(
                [{"role": "system", "content": _SYS}, {"role": "user", "content": goal}])
            if not reply.code:
                ok, note = False, "няма код"
            else:
                res = run_python_subprocess(reply.code + "\n\n" + check)
                ok = res.ok and "CHECK_OK" in res.stdout
                note = "ok" if ok else (res.stderr or "")[:70].replace("\n", " ")
        except Exception as e:
            ok, note = False, f"{type(e).__name__}: {e}"[:70]
        passed += ok
        notes.append(note)
        print(f"  {'✅' if ok else '❌'} [{i}/{len(tasks)}] {goal[:46]:46} {note}", flush=True)
    elapsed = round(time.time() - t0)
    print(f"  → {passed}/{len(tasks)} = {round(100 * passed / len(tasks))}%  ({elapsed}s)",
          flush=True)
    return {"provider": provider, "model": model, "passed": passed, "of": len(tasks),
            "pct": round(100 * passed / len(tasks)), "sec": elapsed, "notes": notes}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("models", nargs="*", help="provider/model (default: the coding chain)")
    ap.add_argument("--tasks", type=int, default=6, help="how many hard tasks (default 6)")
    ap.add_argument("--json", action="store_true", help="also print raw JSON")
    args = ap.parse_args()

    if args.models:
        candidates = []
        for spec in args.models:
            provider, _, model = spec.partition("/")
            if not model:
                print(f"Форматът е provider/model, не {spec!r}")
                return 2
            candidates.append((provider, model))
    else:
        candidates = [(c["provider"], c["model"]) for c in _load_coding_chain()]

    if not candidates:
        print("Няма какво да меря.")
        return 2

    tasks = HARD[:max(1, args.tasks)]
    results = [bench_one(p, m, tasks) for p, m in candidates]

    print("\n\n=== ОБОБЩЕНИЕ ===", flush=True)
    for r in sorted(results, key=lambda r: (-r["pct"], r["sec"])):
        print(f"  {r['pct']:>3}%  {r['passed']}/{r['of']}  {r['sec']:>4}s  "
              f"{r['provider']}/{r['model']}")
    print("\n  Малка извадка — различава счупен от работещ, не трети от четвърти.")
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
