"""Честността A (одит 2026-11-10): срив, скрит от тръба или `|| true`.

`python app.py 2>&1 | tail -5` връща rc на tail — 0, а програмата е паднала с
Traceback; „Работи“ минаваше като подкрепено. Мерено 2026-11-15 с
counts_as_executed: записът беше без `(rc=…)`.
"""
from __future__ import annotations

import pytest

from genesis_agent.claim_check import counts_as_executed

PY_CRASH = ('  File "app.py", line 3, in <module>\n    print(1 / 0)\n'
            "ZeroDivisionError: division by zero\n")
PY_FULL = "Traceback (most recent call last):\n" + PY_CRASH
NODE_CRASH = ("/app/app.js:2\n  throw new Error('boom')\n  ^\n\nError: boom\n"
              "    at Object.<anonymous> (/app/app.js:2:9)\n    at node:internal/main:1:1\n\nNode.js v22.11.0\n")


def _run(command: str, output: str, rc: int = 0) -> str:
    entry = counts_as_executed("RUN_CMD", command, f"[RUN_CMD: {command}]  (rc={rc})\n{output}")
    assert entry is not None
    return entry[1]


@pytest.mark.parametrize("command,output", [
    ("python app.py 2>&1 | tail -5", PY_CRASH),
    ("python app.py 2>&1 | tail -20", PY_FULL),
    ("python app.py || true", PY_FULL),
    ("python app.py; echo done", PY_FULL),
    ("cd app && python main.py 2>&1 | tee run.log", PY_FULL),
    ("node app.js 2>&1 | tail", NODE_CRASH),
])
def test_a_crash_behind_a_pipe_is_not_a_clean_run(command, output):
    assert _run(command, output).endswith("(rc=1)")


@pytest.mark.parametrize("command,output", [
    ("python app.py 2>&1 | tail -5", "Обработени 120 реда\nГотово\n"),
    # logging.exception — програмата продължава и свършва нормално
    ("python app.py 2>&1 | tail", PY_FULL + "retrying…\nDone: 3 files\n"),
    ("python app.py && echo ok", PY_FULL),          # rc=0 на верига с && — програмата е минала
    ("python app.py", PY_FULL),                      # rc е на програмата — вярва му се
    ("grep -r ValueError src | head", "src/x.py: raise ValueError('x')\n"),
])
def test_other_outputs_keep_rc_zero(command, output):
    assert "(rc=" not in _run(command, output)
