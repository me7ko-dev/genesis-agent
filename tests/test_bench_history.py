"""scripts/bench_history.py — the long-turn measurement keeps its invariants."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "bench_history", Path(__file__).resolve().parent.parent / "scripts" / "bench_history.py")
bh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bh)  # type: ignore[union-attr]


def test_a_long_turn_keeps_the_system_prompt_the_task_and_the_last_test_run() -> None:
    res = bh.run(files=8, rounds=40, seed=3)
    assert res["system_kept"] and res["task_kept"] and res["last_test_intact"]
    assert res["last_request_messages"] <= 30


def test_every_request_is_a_valid_history() -> None:
    from genesis_agent import budget
    s = bh.Session(5, 30, 1)
    for i in range(30):
        s.step(i)
        sent = budget.budget_history(s.msgs)
        asked = {tc["id"] for m in sent for tc in m.get("tool_calls") or []}
        assert all(m["tool_call_id"] in asked for m in sent if m.get("role") == "tool")
