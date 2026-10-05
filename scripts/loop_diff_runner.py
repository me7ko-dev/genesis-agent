#!/usr/bin/env python3
"""
Half of scripts/loop_diff.py: runs scripted turns through ONE checkout's
`run_turn` and dumps what the model saw. Started by loop_diff.py in its own
process, with PYTHONPATH and cwd pointing at the checkout under test:

    python scripts/loop_diff_runner.py turns.json out.json

Everything outside the loop is scripted: the model's replies, every tool
result, the browser. Per turn it records every request sent to the model
(the whole message list), the final history and the UI events.
"""
from __future__ import annotations

import json
import sys
import tempfile
from collections import deque
from contextlib import nullcontext
from pathlib import Path

import genesis_skills
import genesis_terminal_agent as gta
from genesis_agent import page_check
from genesis_agent import skill_loader as sl
from genesis_agent.brain import Brain

WORK = Path(tempfile.mkdtemp(prefix="genesis-loop-diff-"))


class _UI(gta.TurnUI):
    def __init__(self, cancel_at: int | None) -> None:
        self.events: list[list] = []
        self.cancel_at = cancel_at
        self.replies = 0

    def thinking(self, label, spinner="dots"):
        self.events.append(["thinking", label, spinner])
        return nullcontext()

    def assistant(self, text):
        self.replies += 1
        self.events.append(["assistant", text])

    def tool(self, name, result):
        self.events.append(["tool", name, result])

    def asked(self, question):
        self.events.append(["asked", question])

    def spinning(self, note):
        self.events.append(["spinning", note])

    def warn(self, text):
        self.events.append(["warn", text])

    def info(self, text):
        self.events.append(["info", text])

    def cancelled(self):
        return self.cancel_at is not None and self.replies >= self.cancel_at


def _tool_result(name: str, args: dict, n: int, rules: dict) -> str:
    rule = rules.get(name, "echo")
    if rule == "ask":
        return f"{genesis_skills.ASK_USER_MARKER}❓ {args.get('question', '?')}"
    if rule == "same":
        return f"[{name}] същото"
    if rule == "blocked":
        return "[SANDBOX BLOCKED] отказано"
    if rule == "py":
        p = WORK / f"m{n}.py"
        p.write_text('print("TOTAL")\n', encoding="utf-8")
        return f"[WRITE_FILE: {p}] ✓ записани 15 символа"
    if rule == "html":
        p = WORK / f"site{n % 2}" / "index.html"
        p.parent.mkdir(exist_ok=True)
        p.write_text("<html></html>", encoding="utf-8")
        return f"[WRITE_FILE: {p}] ✓ записани 13 символа"
    return f"[{name}] резултат {n}: {json.dumps(args, ensure_ascii=False, sort_keys=True)}"


def _text_results(text: str, rules: dict, counter: list[int]) -> list[str]:
    out = []
    for line in text.splitlines():
        if line.startswith("[RUN_CMD:"):
            counter[0] += 1
            out.append(line if rules.get("TEXT") == "same" else f"{line}\nизход {counter[0]}")
        elif line.startswith("[ASK_USER:"):
            out.append(f"{genesis_skills.ASK_USER_MARKER}❓ {line[10:-1].strip()}")
    return out


def _run(turn: dict) -> dict:
    replies = [tuple(r) for r in turn["replies"]]
    requests: list = []
    calls, text_calls = [0], [0]

    def ask(messages, tools=None):
        requests.append(json.loads(json.dumps(list(messages), ensure_ascii=False)))
        return replies.pop(0) if replies else ("Край.", None)

    def dispatch(name, args):
        calls[0] += 1
        return _tool_result(name, args, calls[0], turn["results"])

    gta.ask_genesis = ask
    gta._remember = lambda role, text: None
    gta.HISTORY_DIR = WORK
    genesis_skills.dispatch_tool_call = dispatch
    genesis_skills.parse_and_execute_tools = lambda text: _text_results(text, turn["results"], text_calls)
    Brain.compact_chat_history = staticmethod(lambda messages, **kw: messages)
    page_check.run = lambda page, shots=None: (
        (["контраст 1.5:1"], "") if "site0" in str(page) else ([], ""))
    sl.domain_context = lambda q: turn.get("knowledge", "")
    ui = _UI(turn.get("cancel_at"))
    try:
        out = gta.run_turn(deque([{"role": "system", "content": "S"}], maxlen=30), turn["request"], ui)
        error, final = "", list(out)
    except Exception as e:  # noqa: BLE001 — и провалът се сравнява
        error, final = f"{type(e).__name__}: {e}", []
    return {"requests": requests, "final": final, "events": ui.events, "error": error}


def main() -> int:
    turns = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    text = json.dumps([_run(t) for t in turns], ensure_ascii=False)
    # Всеки процес има своя временна папка, а резултатите носят пътя ѝ.
    for form in (str(WORK), json.dumps(str(WORK))[1:-1]):
        text = text.replace(form, "<WORK>")
    Path(sys.argv[2]).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
