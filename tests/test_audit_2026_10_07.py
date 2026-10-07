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


def test_a_refused_write_to_a_bracketed_path_does_not_count() -> None:
    assert claim_check.executed_from_text_results(
        ["[WRITE_FILE: app/[id]/page.tsx] ❌ Файлът вече съществува"]) == []


# ── браузър: плащания и пароли (одит 2026-10-07, втора вълна) ─────────────────

from genesis_agent import sandbox as _sb


@pytest.mark.parametrize("label", [
    "Завърши поръчката", "Потвърди поръчката", "Към плащане", "Плащане", "Купете", "Платете",
    "Place your order", "Pay $49.99", "Pay", "Checkout", "Proceed to checkout", "Buy",
    "Complete order", "Confirm and pay", "Submit order", "Jetzt kaufen",
    "Zahlungspflichtig bestellen", "Comprar", "Acheter", "🔒 | Pay now",
])
def test_checkout_buttons_are_blocked(label) -> None:
    assert _sb.assess_browser_click(label).level == _sb.RiskLevel.BLOCKED


@pytest.mark.parametrize("label", ["Моите поръчки", "PayPal docs", "Buyer's guide", "Order by date",
                                   "Payments overview", "Вход", "Следваща страница"])
def test_ordinary_buttons_are_not(label) -> None:
    assert _sb.assess_browser_click(label).level == _sb.RiskLevel.CONFIRM


@pytest.mark.parametrize("name", [
    "cc-number", "cc_number", "ccnum", "billing_card_number", "passwd", "user_password", "login_pass",
    "confirmPassword", "Парола", "Номер на карта", "cvv2", "card_cvv", "securityCode", "cc-csc",
])
def test_card_and_password_fields_are_blocked(name) -> None:
    assert _sb.assess_browser_field("text", name).level == _sb.RiskLevel.BLOCKED


def test_autocomplete_alone_blocks_a_field_with_a_meaningless_name() -> None:
    assert _sb.assess_browser_field("text", "field_7", "cc-number").level == _sb.RiskLevel.BLOCKED
    assert _sb.assess_browser_field("text", "field_7", "street-address").level == _sb.RiskLevel.CONFIRM


@pytest.mark.parametrize("name", ["email", "search", "username", "passenger_name", "compass", "Търси в картата"])
def test_ordinary_fields_are_not(name) -> None:
    assert _sb.assess_browser_field("text", name).level == _sb.RiskLevel.CONFIRM


def test_page_check_server_hides_the_projects_secrets(tmp_path) -> None:
    import sys
    import urllib.error
    import urllib.request
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "genesis_agent"))
    import page_check_runner as r
    (tmp_path / "index.html").write_text("<p>x</p>", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-live-123", encoding="utf-8")
    srv, port = r._serve(tmp_path)
    try:
        assert urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html").status == 200
        for path in ("/.env", "/%2eenv", "/.git/config"):
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(f"http://127.0.0.1:{port}{path}")
    finally:
        srv.shutdown()


def test_page_check_env_drops_credentials_in_urls(monkeypatch) -> None:
    from genesis_agent import page_check
    monkeypatch.setenv("DATABASE_URL", "postgres://u:hunter2@db/x")
    monkeypatch.setenv("SOME_URL", "https://u:p@host/")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@proxy:8080")
    env = page_check._clean_env()
    assert "DATABASE_URL" not in env and "SOME_URL" not in env
    assert "HTTPS_PROXY" in env


# ── гейтовете за умения ──────────────────────────────────────────────────────

_BROKEN = "def add(a, b):\n    return a - b\n"


@pytest.mark.parametrize("tail", [
    "def _test():\n    assert add(2, 3) == 5\nprint('OK')",
    "try:\n    assert add(2, 3) == 5\nexcept AssertionError:\n    pass\nprint('OK')",
    "assert 2 + 3 == 5\nprint('OK')",
    "if False:\n    raise SystemExit(1)\nprint('OK')",
])
def test_a_check_that_never_runs_is_not_a_self_test(tail) -> None:
    from genesis_agent.verifier import verify_skill
    assert verify_skill(_BROKEN + tail).method != "self_test_passed"


def test_a_check_in_a_called_or_nested_async_function_counts() -> None:
    import ast

    from genesis_agent.verifier import _has_real_check
    code = ("import asyncio\nasync def f():\n    return 1\nif __name__ == '__main__':\n"
            "    async def _run():\n        assert await f() == 1\n    asyncio.run(_run())\n    print('OK')\n")
    assert _has_real_check(ast.parse(code))


def test_research_does_not_count_not_found_as_a_source(monkeypatch) -> None:
    from genesis_agent import research as rs

    class _B:
        def __init__(self) -> None:
            self.replies = ["X is 42", "НЕ Е ОТКРИТО В ТОЗИ ИЗТОЧНИК", "НЕ Е ОТКРИТО В ТОЗИ ИЗТОЧНИК"]

        def complete(self, messages):
            return type("R", (), {"raw_text": self.replies.pop(0)})()
    brain = _B()
    monkeypatch.setattr("genesis_agent.brain.Brain", lambda: brain)
    monkeypatch.setattr("genesis_agent.web_search.search", lambda *a, **k: [
        {"title": t, "url": f"https://{t}.test", "snippet": "s"} for t in "abc"])
    out = rs.grounded_research("what is X")
    assert "само 1 източник" in out and "проверено през" not in out
