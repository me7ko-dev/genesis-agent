"""Regressions for the core-loop bugs an audit reproduced on 2026-10-07."""
from __future__ import annotations

from collections import deque

import pytest

from genesis_agent import claim_check, repeat_guard
from genesis_agent.code_check import RunCheck
from genesis_agent.tool_schemas import load_tool_arguments

# ── 1. compaction never leaves a tool result without its call ────────────────

def _rounds(n: int) -> list[dict]:
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    k = 0
    for _ in range(n):
        ids = [f"c{k + j}" for j in range(3)]
        k += 3
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": i, "type": "function",
                                     "function": {"name": "READ_FILE", "arguments": "{}"}} for i in ids]})
        msgs += [{"role": "tool", "tool_call_id": i, "name": "READ_FILE", "content": "x"} for i in ids]
    msgs.append({"role": "assistant", "content": "готово"})
    return msgs


def _orphans(msgs: list[dict]) -> list[str]:
    asked: set[str] = set()
    out = []
    for m in msgs:
        for tc in m.get("tool_calls") or []:
            asked.add(tc["id"])
        if m.get("role") == "tool" and m["tool_call_id"] not in asked:
            out.append(m["tool_call_id"])
    return out


def test_compaction_cuts_before_the_call_not_between_its_results(monkeypatch) -> None:
    from genesis_agent import brain as brain_mod

    class _Reply:
        raw_text = "резюме"
    monkeypatch.setattr(brain_mod.Brain, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(brain_mod.Brain, "complete", lambda self, m, tools=None: _Reply())
    out = brain_mod.Brain.compact_chat_history(deque(_rounds(4), maxlen=30), 16, 10)
    assert _orphans(list(out)) == []
    assert list(out)[2]["role"] != "tool"


# ── 2. an empty reply with no tool calls ends the turn, not the program ──────

def test_run_turn_survives_an_empty_reply(monkeypatch) -> None:
    import genesis_terminal_agent as gta
    monkeypatch.setattr(gta, "ask_genesis", lambda m, tools=None: ("", None))

    class UI(gta.TurnUI):
        def thinking(self, label, spinner="dots"):
            import contextlib
            return contextlib.nullcontext()
        def assistant(self, text): pass
        def tool(self, name, result): pass
        def asked(self, question): pass
        def spinning(self, note): pass
        def warn(self, text): pass
        def info(self, text): pass
        def cancelled(self): return False

    out = gta.run_turn(deque([{"role": "system", "content": "s"}], maxlen=30), "здравей", UI())
    assert out[-1]["role"] == "assistant"


# ── 3. a refused write is not a write ────────────────────────────────────────

@pytest.mark.parametrize("result", [
    "[WRITE_FILE: /ws/app.py] ❌ Файлът вече съществува и не си го чел в тази сесия",
    "[WRITE_FILE] Грешка: [Errno 13] Permission denied",
    "[EDIT_FILE: /ws/app.py] ❌ Anchor-ът не е намерен",
])
def test_a_refused_write_does_not_count(result) -> None:
    assert claim_check.executed_from_text_results([result]) == []
    assert claim_check.counts_as_executed("WRITE_FILE", "/ws/app.py", result) is None


def test_a_successful_write_still_counts() -> None:
    assert claim_check.executed_from_text_results(["[WRITE_FILE: /ws/a.py] ✓ записани 3 символа"])


# ── 7. test claims: past tense only; a failing test run is still a run ──────

@pytest.mark.parametrize("text", [
    "Make sure the tests pass after you pull.",
    "Пусни pytest локално, за да видиш дали тестовете минават.",
])
def test_advice_about_tests_is_not_a_claim(text) -> None:
    assert claim_check.unsupported_claims(text, []) == []


@pytest.mark.parametrize("text", ["All tests pass now.", "Тестовете минаха.", "Пуснах тестовете."])
def test_claims_about_tests_still_need_a_run(text) -> None:
    assert claim_check.unsupported_claims(text, [])


def test_a_failing_test_run_counts_as_run() -> None:
    result = ("[RUN_CMD: python -m unittest test_calc.py]  (rc=1)\n"
              "Traceback (most recent call last):\n  ...\nAssertionError")
    entry = claim_check.counts_as_executed("RUN_CMD", "python -m unittest test_calc.py", result)
    assert entry is not None
    assert claim_check.unsupported_claims("Пуснах тестовете: test_div пада.", [entry]) == []


# ── 4. JSON repair: \b is a regex word boundary, \d a digit class ────────────

def test_repair_keeps_regex_word_boundaries() -> None:
    raw = r'{"path": "w.py", "content": "WORD = re.compile(r\"\bcat\d+\b\")\n"}'
    assert load_tool_arguments(raw)["content"] == 'WORD = re.compile(r"\\bcat\\d+\\b")\n'


def test_repair_handles_latex_backslash_u() -> None:
    raw = r'{"path": "doc.tex", "content": "\documentclass{article}\n\usepackage{x}\n"}'
    assert load_tool_arguments(raw)["path"] == "doc.tex"


def test_valid_json_is_untouched() -> None:
    assert load_tool_arguments('{"a": "x\\by\\u0436"}')["a"] == "x\byж"


# ── 5/6. work memory dedup ──────────────────────────────────────────────────

@pytest.fixture
def wm(tmp_path, monkeypatch):
    from genesis_agent import workspace_memory
    monkeypatch.setattr(workspace_memory, "DB_PATH", tmp_path / "wm.db")
    workspace_memory.set_workspace(tmp_path)
    yield workspace_memory
    workspace_memory.set_workspace(None)


def test_cyrillic_titles_differing_in_case_are_one_thread(wm) -> None:
    wm.add_thread("Миграция към Postgres", "напиши alembic скрипта")
    assert "вече съществува" in wm.add_thread("миграция към postgres")
    threads = wm.list_threads("open")
    assert len(threads) == 1
    assert threads[0]["next_step"] == "напиши alembic скрипта"     # празното не трие


def test_a_closed_thread_does_not_swallow_new_work(wm) -> None:
    wm.add_thread("Обнови зависимостите")
    wm.close_thread(wm.list_threads("open")[0]["id"])
    assert "✓" in wm.add_thread("Обнови зависимостите", "requests има CVE")
    assert len(wm.list_threads("open")) == 1


@pytest.mark.parametrize(("a", "b"), [
    ("Бекендът се пише на C#", "Бекендът се пише на C++"),
    ("Таймаут: 1.5 секунди", "Таймаут: 15 секунди"),
])
def test_decisions_differing_in_symbols_are_distinct(wm, a, b) -> None:
    wm.add_decision(a)
    assert "✓" in wm.add_decision(b)


def test_same_decision_with_a_trailing_period_is_a_duplicate(wm) -> None:
    wm.add_decision("Комитите са на български")
    assert "Вече е записано" in wm.add_decision("комитите са на български.")


# ── 8. guards that fired on legitimate work ──────────────────────────────────

def test_rewriting_a_file_in_text_mode_is_not_spinning() -> None:
    g = repeat_guard.RepeatGuard()
    for _ in range(4):
        v = g.observe_text_result("[WRITE_FILE: /ws/app.py] ✓ записани 412 символа")
    assert not v.stop and not v.note


def test_a_versioned_python_counts_as_running_the_file() -> None:
    c = RunCheck()
    c.observe("[WRITE_FILE: /ws/calc.py] ✓ записани 30 символа")
    c.observe("[RUN_CMD: python3.12 calc.py]  (rc=0)\nok")
    assert "НЕ пуснат" not in c.note()
