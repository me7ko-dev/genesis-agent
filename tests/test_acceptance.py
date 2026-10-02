"""genesis_agent.acceptance — tests written from the request alone (NEXT_STEPS Б.4)."""
from genesis_agent import acceptance as acc

MODULE = "def vat(net):\n    return round(net * 0.2, 2)\n"
TESTS = ("```python\nfrom money import vat\n\n\ndef test_vat_20_percent():\n    assert vat(100) == 20\n\n\n"
         "def test_total_with_vat():\n    from money import total\n    assert total(100) == 120\n```")


def _check(tmp_path, monkeypatch, reply=TESTS, module=MODULE):
    monkeypatch.setenv("GENESIS_ACCEPTANCE", "1")
    (tmp_path / "money.py").write_text(module, encoding="utf-8")
    calls = []

    def fake(messages):
        calls.append(messages)
        return reply

    c = acc.AcceptanceCheck("Направи money.py с vat(net) и total(net) — сумата с 20% ДДС.", complete=fake)
    c.observe(f"[WRITE_FILE: {tmp_path / 'money.py'}] ✓ записани 40 символа")
    return c, calls


def test_failures_go_back_once_with_the_task_first_rule(tmp_path, monkeypatch):
    c, calls = _check(tmp_path, monkeypatch)
    assert c.due()
    note = c.check()
    assert "1/2" in note and "test_total_with_vat" in note and "условието" in note
    assert not c.due() and c.check() == ""
    sent = calls[0][-1]["content"]
    assert "total(net)" in sent and "round(net" not in sent  # заявката, никога кодът


def test_all_green_says_nothing(tmp_path, monkeypatch):
    c, _ = _check(tmp_path, monkeypatch, module=MODULE + "\n\ndef total(net):\n    return net + vat(net)\n")
    assert c.check() == ""


def test_off_by_default(tmp_path, monkeypatch):
    c, _ = _check(tmp_path, monkeypatch)
    monkeypatch.delenv("GENESIS_ACCEPTANCE")
    assert not c.due()


def test_no_names_in_the_request_or_a_failed_call_costs_nothing_more(tmp_path, monkeypatch):
    for reply in ("NONE", "Error: all models failed", "no code here"):
        c, _ = _check(tmp_path, monkeypatch, reply=reply)
        assert c.check() == "" and not c.due()


def test_nothing_written_nothing_due(monkeypatch):
    monkeypatch.setenv("GENESIS_ACCEPTANCE", "1")
    c = acc.AcceptanceCheck("здрасти", complete=lambda m: TESTS)
    c.observe("[READ_FILE: a.py]\nx = 1")
    assert not c.due()


def test_the_project_root_is_above_tests(tmp_path):
    assert acc.project_root(tmp_path / "tests" / "test_money.py") == tmp_path
    assert acc.project_root(tmp_path / "money.py") == tmp_path


def test_model_written_tests_never_see_the_api_keys(tmp_path, monkeypatch):
    """Тестовете ги пише моделът — средата е тази на RUN_CMD, не на Genesis."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    reply = ("```python\nimport os\nfrom money import vat\n\n\ndef test_no_key():\n"
             "    assert vat(100) == 20\n    assert 'OPENROUTER_API_KEY' not in os.environ\n```")
    c, _ = _check(tmp_path, monkeypatch, reply=reply)
    assert c.check() == ""


def _loop(tmp_path, monkeypatch):
    from genesis_agent import agent_core as ac
    monkeypatch.setattr("genesis_agent.brain.Brain.compact_chat_history",
                        staticmethod(lambda messages, threshold=16, keep_recent=10: messages))
    monkeypatch.setattr(acc, "_brain_complete", lambda messages: TESTS)
    (tmp_path / "money.py").write_text(MODULE, encoding="utf-8")
    tc = [{"id": "1", "function": {"name": "WRITE_FILE", "arguments": "{}"}}]
    replies = [("", tc, "p", "m"), ("Готово.", None, "p", "m"), ("Пробвах го.", None, "p", "m")]

    class Core:
        def complete(self, messages):
            return replies.pop(0) if replies else ("Край.", None, "p", "m")

        def remember(self, *a):
            pass

    class Skills:
        def dispatch_tool_call(self, name, args):
            return f"[WRITE_FILE: {tmp_path / 'money.py'}] ✓ записани 40 символа"

        def parse_and_execute_tools(self, text):
            return []

    core = Core()
    core.skills = Skills()
    shown = []
    messages = ac.run_tool_loop(core, [{"role": "user", "content": "money.py с vat(net) и total(net)"}],
                                on_assistant=lambda *a: None,
                                on_tool_result=lambda name, res, extra: shown.append((name, res)))
    return [m["content"] for m in messages if m["role"] == "system"], shown


def test_the_loop_sends_failures_back_when_on(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_ACCEPTANCE", "1")
    notes, shown = _loop(tmp_path, monkeypatch)
    assert any("[приемни тестове]" in n and "1/2" in n for n in notes)
    assert any(name == "приемни тестове" for name, _ in shown)


def test_the_loop_is_unchanged_when_off(tmp_path, monkeypatch):
    monkeypatch.delenv("GENESIS_ACCEPTANCE", raising=False)
    notes, shown = _loop(tmp_path, monkeypatch)
    assert not any("[приемни тестове]" in n for n in notes)
    assert not any(name == "приемни тестове" for name, _ in shown)


def _terminal_turn(tmp_path, monkeypatch):
    from collections import deque

    import genesis_terminal_agent as gta
    monkeypatch.setattr(gta, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    monkeypatch.setattr("genesis_agent.skill_loader.domain_context", lambda q: "")
    monkeypatch.setattr(acc, "_brain_complete", lambda messages: TESTS)
    (tmp_path / "money.py").write_text(MODULE, encoding="utf-8")
    call = {"id": "1", "type": "function", "function": {"name": "WRITE_FILE", "arguments": "{}"}}
    replies = [("", [call]), ("Готово.", [])]
    monkeypatch.setattr(gta, "ask_genesis", lambda *a, **k: replies.pop(0) if replies else ("Край.", []))
    monkeypatch.setattr(gta.genesis_skills, "dispatch_tool_call",
                        lambda name, args: f"[WRITE_FILE: {tmp_path / 'money.py'}] ✓ записани 40 символа")
    monkeypatch.setattr(gta, "parse_and_execute_tools", lambda text: [])

    class NoRunCheck:
        def observe(self, result):
            pass

        def due(self):
            return False

    monkeypatch.setattr(gta, "_RunCheck", NoRunCheck)
    shown: list[str] = []

    class UI(gta.TurnUI):
        def tool(self, name, result):
            shown.append(name)

    messages = gta.run_turn(deque([{"role": "system", "content": "s"}], maxlen=50),
                            "money.py с vat(net) и total(net)", UI())
    return [m["content"] for m in messages if m["role"] == "system"], shown


def test_the_terminal_turn_runs_them_too(tmp_path, monkeypatch):
    """bench_projects пуска терминала (cli → run_turn), а проверката живееше само в
    agent_core.run_tool_loop — с GENESIS_ACCEPTANCE=1 bench-ът мереше същото (2026-10-02)."""
    monkeypatch.setenv("GENESIS_ACCEPTANCE", "1")
    notes, shown = _terminal_turn(tmp_path, monkeypatch)
    assert any("[приемни тестове]" in n and "1/2" in n for n in notes)
    assert "приемни тестове" in shown


def test_the_terminal_turn_is_unchanged_when_off(tmp_path, monkeypatch):
    monkeypatch.delenv("GENESIS_ACCEPTANCE", raising=False)
    notes, shown = _terminal_turn(tmp_path, monkeypatch)
    assert not any("[приемни тестове]" in n for n in notes) and "приемни тестове" not in shown


def test_the_tests_run_through_the_sandbox_limits(tmp_path, monkeypatch):
    """Същият път като RUN_CMD: лимити, таймаут на цялото дърво, чиста среда."""
    from genesis_agent import sandbox
    real, seen = sandbox._run, []
    monkeypatch.setattr(sandbox, "_run", lambda argv, **kw: seen.append(kw) or real(argv, **kw))
    c, _ = _check(tmp_path, monkeypatch)
    assert "1/2" in c.check()
    assert seen and seen[0]["timeout"] == 120 and seen[0]["env_extra"] == {"PYTHONPATH": str(tmp_path)}
