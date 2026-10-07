#!/usr/bin/env python3
"""
What a long project turn costs, and what the model still sees at its end.

    python scripts/bench_history.py                 # 5 files, 24 rounds
    python scripts/bench_history.py --files 8 --rounds 40

NEXT_STEPS Г.12. Offline, no model: a scripted turn builds a project the way
the chat does — WRITE_FILE per file (the content travels in the assistant's
tool_call arguments), READ_FILE before an edit, EDIT_FILE, pytest after each
change with a failing run now and then. Every request goes through exactly
what Brain.complete sends (budget.budget_history → request_window). Reported
per request: estimated prompt tokens and messages; at the end: is the system
prompt there, is the task there, how many files can the model still see in
their CURRENT form (a write or a read of the latest version, not clipped), and
is the latest test output intact.

Token estimate = characters / 3.5 (the mix of code, English and Bulgarian in a
real session; budget_log measures ~3.3-4).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genesis_agent import budget  # noqa: E402

SYSTEM = "x" * 7600          # ~2 200 tokens: system prompt + schemas, measured in PR #7
TASK = ("Направи в текущата папка Python проект за фактури: парсване на PDF, валидиране на ЕИК, "
        "изчисляване на ДДС, експорт в Excel и CLI. Добави тестове с pytest и ги пусни.")


def _file_body(name: str, version: int, size: int, rng: random.Random) -> str:
    lines = [f"# {name} v{version}", "from __future__ import annotations", ""]
    i = 0
    while sum(len(x) + 1 for x in lines) < size:
        lines += [f"def f_{version}_{i}(value: int) -> int:",
                  f'    """Step {i} of {name}."""',
                  f"    return value * {rng.randint(2, 99)} + {i}", ""]
        i += 1
    return "\n".join(lines)


def _pytest_output(ok: bool, files: list[str], rng: random.Random) -> str:
    if ok:
        return f"{'.' * rng.randint(8, 30)}\n{rng.randint(8, 30)} passed in 0.{rng.randint(1, 9)}s"
    tb = "\n".join(f"  File \"{rng.choice(files)}\", line {rng.randint(1, 80)}, in f\n    x = y"
                   for _ in range(12))
    return f"F....\n=== FAILURES ===\n{tb}\nAssertionError: 41 != 42\n1 failed, 9 passed in 0.4s"


class Session:
    def __init__(self, files: int, rounds: int, seed: int) -> None:
        self.rng = random.Random(seed)
        self.names = [f"faktura/{n}.py" for n in
                      ("parse", "eik", "vat", "excel", "cli", "models", "storage", "report",
                       "config", "utils")[:files]]
        self.rounds = rounds
        self.msgs: list[dict] = [{"role": "system", "content": SYSTEM},
                                 {"role": "user", "content": TASK}]
        self.current: dict[str, str] = {}
        self.k = 0
        self.last_test = ""

    def _call(self, name: str, args: dict, result: str) -> None:
        cid = f"c{self.k}"
        self.k += 1
        self.msgs.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]})
        self.msgs.append({"role": "tool", "tool_call_id": cid, "name": name,
                          "content": budget.clip_for_context(result)})

    def step(self, i: int) -> None:
        rng = self.rng
        if i < len(self.names):                                  # write each file once
            name = self.names[i]
            body = _file_body(name, 1, rng.randint(1500, 4000), rng)
            self.current[name] = body
            self._call("WRITE_FILE", {"path": name, "content": body},
                       f"[WRITE_FILE: /ws/{name}] ✓ записани {len(body)} символа")
            return
        name = rng.choice(self.names)
        kind = i % 3
        if kind == 0:                                             # read before an edit
            self._call("READ_FILE", {"path": name}, f"[READ_FILE: /ws/{name}]\n{self.current[name]}")
        elif kind == 1:                                           # edit
            old = self.current[name].splitlines()[3]
            new = old + "  # fixed"
            self.current[name] = self.current[name].replace(old, new, 1)
            self._call("EDIT_FILE", {"path": name, "old_string": old, "new_string": new},
                       f"[EDIT_FILE: /ws/{name}] ✓ заменено (1)\n--- a\n+++ b\n-{old}\n+{new}")
        else:                                                     # run tests
            out = _pytest_output(rng.random() < 0.6, self.names, rng)
            self.last_test = out
            self._call("RUN_CMD", {"command": "python -m pytest -q"},
                       f"[RUN_CMD: python -m pytest -q]  (rc={0 if 'failed' not in out else 1})\n{out}")


def _visible(sent: list[dict], name: str, body: str) -> bool:
    for m in sent:
        if m.get("role") == "tool" and body in str(m.get("content", "")):
            return True
        for tc in m.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"]["arguments"])
            except (ValueError, KeyError, TypeError):
                continue
            if args.get("path") == name and args.get("content") == body:
                return True
    return False


def run(files: int, rounds: int, seed: int) -> dict:
    s = Session(files, rounds, seed)
    per_request = []
    sent: list[dict] = []
    for i in range(rounds):
        s.step(i)
        sent = budget.budget_history(s.msgs)
        chars = sum(len(str(m.get("content", ""))) + sum(len(tc["function"]["arguments"])
                    for tc in m.get("tool_calls") or []) for m in sent)
        args_chars = sum(len(tc["function"]["arguments"]) for m in sent for tc in m.get("tool_calls") or [])
        per_request.append({"round": i + 1, "messages": len(sent), "tokens": round(chars / 3.5),
                            "in_tool_call_args": round(args_chars / 3.5)})
    seen = [n for n in s.names if _visible(sent, n, s.current[n])]
    return {
        "rounds": rounds, "files": files,
        "total_prompt_tokens": sum(r["tokens"] for r in per_request),
        "last_request_tokens": per_request[-1]["tokens"],
        "max_request_tokens": max(r["tokens"] for r in per_request),
        "last_request_messages": per_request[-1]["messages"],
        "share_in_tool_call_args": round(per_request[-1]["in_tool_call_args"] / max(per_request[-1]["tokens"], 1), 2),
        "system_kept": sent[0]["content"] == SYSTEM,
        "task_kept": any(m.get("role") == "user" and m.get("content") == TASK for m in sent),
        "files_current_visible": f"{len(seen)}/{len(s.names)}",
        "last_test_intact": bool(s.last_test) and any(s.last_test in str(m.get("content", "")) for m in sent),
        "per_request": per_request,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--files", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=24)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    a = ap.parse_args()
    res = run(max(1, min(a.files, 10)), max(a.files, a.rounds), a.seed)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"{res['files']} файла, {res['rounds']} рунда в един ход")
    for r in res["per_request"][:: max(1, len(res["per_request"]) // 8)]:
        print(f"  рунд {r['round']:>3}: {r['messages']:>3} съобщения, ~{r['tokens']:>6} токена "
              f"({r['in_tool_call_args']} в аргументите на извикванията)")
    print(f"Общо промпт за хода: ~{res['total_prompt_tokens']} токена; "
          f"последната заявка ~{res['last_request_tokens']} (макс. ~{res['max_request_tokens']})")
    print(f"Делът на аргументите (съдържание на WRITE_FILE/EDIT_FILE): {res['share_in_tool_call_args']:.0%}")
    print(f"Системен промпт: {'✓' if res['system_kept'] else '✗'}  Задача: {'✓' if res['task_kept'] else '✗'}  "
          f"Файлове в текущия си вид: {res['files_current_visible']}  "
          f"Последният pytest: {'✓' if res['last_test_intact'] else '✗'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
