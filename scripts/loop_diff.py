#!/usr/bin/env python3
"""
Does a change to the turn loop change anything the model sees?

    python scripts/loop_diff.py OLD_CHECKOUT [NEW_CHECKOUT] [--turns N] [--seed S]

The bench answers "does it work better?" with real models, and its noise is
larger than any refactor: the same code scored 4/10 and 8/11 on one day.
This answers the other question deterministically. It generates N random
turns (seeded): native tool calls, text tags, ASK_USER, repeated results,
blocked commands, written .py and .html files, Stop, verified knowledge,
claims, promises, malformed tags. Each checkout runs them in its own process
(loop_diff_runner.py) with the same scripted model, and every request sent to
the model, the final history and the UI events (tool panels aside) must be
identical. NEW_CHECKOUT defaults to this one.

Known, intended differences since f568d3f (2026-10-04) are normalized out:
- the malformed-tag note's typo, "формàт" -> "формат"
- tool panels: text-tag results are shown, the ASK_USER marker is not
- an empty reply with no tool calls crashed the old loop (counted apart;
  until the crash the requests must match)

Exit code 0 = no differences. Measured 2026-10-04, f568d3f vs 0e8a45f:
500 turns, 459 identical, 41 crashes of the old loop, 0 different.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNNER = Path(__file__).resolve().parent / "loop_diff_runner.py"

_REQUESTS = ["направи задачата", "report.py печата всеки продукт и ред ОБЩО",
             "инсталирай пакета requests", "направи сайт за къща за гости"]
_TEXT = ["Готово.", "Инсталирах пакета.", "Сега ще създам файла.", "[INSTALL: requests]",
         "", "Проверих го в браузъра, няма грешки.", "Разгледах. Това е всичко.",
         "[RUN_CMD: ls]", "[RUN_CMD: pip install requests]", "[RUN_CMD: ls]\n[ASK_USER: кой порт?]"]
_NAMES = ["LIST_DIR", "RUN_CMD", "WRITE_FILE", "READ_FILE", "ASK_USER"]
_RULES = ["echo", "echo", "same", "ask", "blocked", "py", "html"]


def make_turns(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)

    def call(i: int) -> dict:
        if rng.random() < 0.05:
            arguments = "{не е json"
        elif rng.random() < 0.5:
            arguments = json.dumps({"path": "."})
        else:
            arguments = json.dumps({"command": f"echo {rng.randint(0, 3)}", "question": "кой?"})
        return {"id": f"c{i}", "type": "function",
                "function": {"name": rng.choice(_NAMES), "arguments": arguments}}

    turns = []
    for _ in range(n):
        replies: list = []
        for i in range(rng.randint(1, 14)):
            if rng.random() < 0.45:
                replies.append([rng.choice(["", "гледам"]),
                                [call(i * 10 + k) for k in range(rng.choice([1, 1, 2]))]])
            else:
                replies.append([rng.choice(_TEXT), rng.choice([None, None, []])])
        turns.append({
            "request": rng.choice(_REQUESTS),
            "knowledge": rng.choice(["", "", "Проверено знание: ЕГН\nконтролна цифра"]),
            "cancel_at": rng.choice([None] * 12 + [1, 2]),
            "results": {name: rng.choice(_RULES) for name in _NAMES}
            | {"ASK_USER": rng.choice(["ask", "echo"]), "TEXT": rng.choice(["echo", "same"])},
            "replies": replies,
        })
    return turns


def run_checkout(checkout: Path, turns_file: Path, out: Path) -> list[dict]:
    env = dict(os.environ, PYTHONPATH=str(checkout), PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
               GENESIS_MEMORY_DIR=tempfile.mkdtemp(prefix="genesis-loop-diff-mem-"))
    env.pop("GENESIS_ACCEPTANCE", None)
    subprocess.run([sys.executable, str(RUNNER), str(turns_file), str(out)], cwd=checkout,
                   env=env, check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(out.read_text(encoding="utf-8"))


def _norm(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False).replace("формàт", "формат"))


def compare(old: list[dict], new: list[dict]) -> tuple[int, int, list[int]]:
    """(identical, old crashes on an empty reply, indexes that differ)."""
    same, crashed, differ = 0, 0, []
    for i, (o, n) in enumerate(zip(old, new)):
        if o["error"] and "has no len" in o["error"] and not n["error"]:
            crashed += 1
            if _norm(o["requests"]) != _norm(n["requests"][:len(o["requests"])]):
                differ.append(i)
            continue
        o_events = [e for e in o["events"] if e[0] != "tool"]
        n_events = [e for e in n["events"] if e[0] != "tool"]
        if ((_norm(o["requests"]), _norm(o["final"]), _norm(o_events), o["error"])
                == (_norm(n["requests"]), _norm(n["final"]), _norm(n_events), n["error"])):
            same += 1
        else:
            differ.append(i)
    return same, crashed, differ


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("old", type=Path, help="the checkout before the change")
    ap.add_argument("new", type=Path, nargs="?", default=REPO, help="default: this checkout")
    ap.add_argument("--turns", type=int, default=500)
    ap.add_argument("--seed", type=int, default=20261004)
    args = ap.parse_args(argv)
    sys.path.insert(0, str(REPO))
    from genesis_agent.paths import ensure_utf8_streams
    ensure_utf8_streams()  # конзолата на Windows е cp1251

    turns = make_turns(args.turns, args.seed)
    tmp = Path(tempfile.mkdtemp(prefix="genesis-loop-diff-"))
    turns_file = tmp / "turns.json"
    turns_file.write_text(json.dumps(turns, ensure_ascii=False), encoding="utf-8")
    old = run_checkout(args.old.resolve(), turns_file, tmp / "old.json")
    new = run_checkout(args.new.resolve(), turns_file, tmp / "new.json")
    same, crashed, differ = compare(old, new)
    print(f"{len(turns)} хода: {same} еднакви, {crashed} сриват стария цикъл (празен отговор), "
          f"{len(differ)} различни")
    for i in differ[:3]:
        print(f"  ход {i}: {json.dumps(turns[i], ensure_ascii=False)[:300]}")
    return 1 if differ else 0


if __name__ == "__main__":
    raise SystemExit(main())
