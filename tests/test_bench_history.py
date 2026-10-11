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


def _calls(s) -> list[tuple[str, dict]]:
    import json
    return [(tc["function"]["name"], json.loads(tc["function"]["arguments"]))
            for m in s.msgs for tc in m.get("tool_calls") or []]


def _played(rounds: int = 24):
    s = bh.Session(5, rounds, 7)
    for i in range(rounds):
        s.step(i)
    return s


def test_edit_file_uses_the_real_schema_keys() -> None:
    """2026-10-11: bench пращаше {old_string, new_string}, а tool_schemas и
    genesis_skills._dispatch ползват {path, old, new}."""
    edits = [a for n, a in _calls(_played()) if n == "EDIT_FILE"]
    assert edits and all(set(a) == {"path", "old", "new"} for a in edits)


def test_a_file_is_rewritten_later_in_the_turn() -> None:
    """Иначе правилото „по-нов запис заменя стария“ няма какво да мери."""
    s = _played()
    writes = [(a["path"], a["content"]) for n, a in _calls(s) if n == "WRITE_FILE"]
    paths = [p for p, _ in writes]
    assert len(paths) > len(set(paths))
    for p in {p for p in paths if paths.count(p) > 1}:
        last = [c for q, c in writes if q == p][-1]
        assert " v1\n" not in last.splitlines()[0] + "\n"


def test_elide_saves_tokens_and_keeps_the_invariants(monkeypatch) -> None:
    """B.12 (2026-10-11): с изпускане на старите записи ходът е по-евтин, а
    системният промпт, задачата и последният pytest остават."""
    from genesis_agent import budget
    off = bh.run(files=5, rounds=24, seed=7)
    monkeypatch.setattr(budget, "ELIDE_OLD_WRITES", 1)
    monkeypatch.setattr(budget, "ELIDE_WRITE_ROUNDS", 6)
    on = bh.run(files=5, rounds=24, seed=7)
    assert on["total_prompt_tokens"] < off["total_prompt_tokens"] * 0.85
    assert on["system_kept"] and on["task_kept"] and on["last_test_intact"]
    s = bh.Session(5, 24, 7)
    for i in range(24):
        s.step(i)
        sent = budget.budget_history(s.msgs)
        asked = {tc["id"] for m in sent for tc in m.get("tool_calls") or []}
        assert all(m["tool_call_id"] in asked for m in sent if m.get("role") == "tool")


def test_visibility_is_reported_per_request() -> None:
    res = bh.run(files=5, rounds=15, seed=7)
    assert all("files_visible" in r for r in res["per_request"])
    assert 0.0 <= res["files_visible_mean"] <= 1.0
