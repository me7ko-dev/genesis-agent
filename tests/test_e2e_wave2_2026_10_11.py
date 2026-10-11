"""E2E проверката 2026-10-11 (вълна 2): пазачът без
ключ при доставчик извън Brain, режим план, „тестовете минават“ след червен
pytest, отговорът на текстов ASK_USER, кирилски команди. Всеки тест пада без
поправката си (пуснато срещу кода преди нея)."""
from __future__ import annotations

import contextlib
import json
from collections import deque

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import hooks


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    monkeypatch.setattr(gta, "WORKSPACE", root)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    gs._SEEN_PATHS.clear()
    hooks.workspace = root
    yield root
    hooks.workspace = None
    gs._set_agent_scope(None)
    gs._set_cancel(None)


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _script(monkeypatch, replies):
    it = iter(replies)
    seen: list[list[dict]] = []

    def fake(messages, tools=None):
        seen.append([dict(m) for m in messages])
        return next(it)
    monkeypatch.setattr(gta, "ask_genesis", fake)
    return seen


class _UI(gta.TurnUI):
    def __init__(self):
        self.log: list = []

    def thinking(self, label, spinner="dots"):
        return contextlib.nullcontext()

    def assistant(self, text): self.log.append(("assistant", text))
    def tool(self, name, result): self.log.append(("tool", name, result))
    def asked(self, q): self.log.append(("asked", q))
    def spinning(self, n): self.log.append(("spin", n))
    def warn(self, t): self.log.append(("warn", t))
    def info(self, t): self.log.append(("info", t))


def _fresh():
    return deque([{"role": "system", "content": "SYS"}], maxlen=gta._HISTORY_MAXLEN)


# ── F2: пазачът „няма ключ“ не спира доставчик извън Brain ──────────────────

@pytest.mark.parametrize("prov, model", [("github", "gpt-4o"), ("llmstudio", "local-model"),
                                         ("ollama", "qwen3:8b")])
def test_the_no_key_guard_lets_a_terminal_only_provider_answer(monkeypatch, prov, model):
    from genesis_agent import brain
    monkeypatch.setattr(brain, "any_model_reachable", lambda: False)
    monkeypatch.delenv("GENESIS_LOCAL_ONLY", raising=False)
    monkeypatch.setattr(gta, "current_provider", prov)
    monkeypatch.setattr(gta, "current_model_id", model)
    assert gta._no_model_guard() is False


def test_the_no_key_guard_still_stops_a_brain_provider(monkeypatch, capsys):
    from genesis_agent import brain
    monkeypatch.setattr(brain, "any_model_reachable", lambda: False)
    monkeypatch.setattr(gta, "current_provider", "groq")
    monkeypatch.setattr(gta.sys.stdin, "isatty", lambda: False, raising=False)
    assert gta._no_model_guard() is True


# ── F4: „създадох план“ в режим план е самият план, не твърдение за запис ───

_PLAN = ("Прочетох app.py и създадох план:\n1. В app.py ще добавя функция vat(price).\n"
         "2. Ще напиша test_app.py и ще пусна pytest.\nРиск: закръгляне.")


def test_headless_plan_that_says_it_made_a_plan_is_the_answer(ws, monkeypatch, capsys):
    from genesis_agent import headless
    (ws / "app.py").write_text("PRICE = 42\n", encoding="utf-8")
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))
    monkeypatch.setattr(headless, "_read_stdin", lambda: "")
    seen = _script(monkeypatch, [("", [_call("READ_FILE", {"path": "app.py"}, "r1")]),
                                 (_PLAN, None), ("План: (повторен)", None)])
    code = headless.run(["--plan", "--json", "--cwd", str(ws), "добави ДДС към app.py"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["result"].startswith("Прочетох app.py и създадох план")
    assert out["warnings"] == [] and len(seen) == 2


# ── F5: „тестовете минават“ след червен pytest — честите форми и exit кодът ─

_RED = [("RUN_CMD", "python -m pytest -q (rc=1)")]


@pytest.mark.parametrize("text", [
    "Готово! Всички 5 теста минават.", "Тестовете минават.", "All 5 tests pass.",
    "Всичките тестове минаха успешно.", "Всички тестове преминават успешно."])
def test_counted_and_plain_test_pass_claims_are_caught_after_a_red_run(text):
    from genesis_agent import claim_check
    claims = claim_check.unsupported_claims(text, _RED)
    assert claims and claim_check.green_claim_after_red(claims, _RED) == 1


@pytest.mark.parametrize("text", [
    "Провери дали тестовете минават.", "Ако тестовете минават, готово.",
    "Резултат: 1 failed, 4 passed.", "4 passed, 1 failed", "Пуснах тестовете: test_a пада.",
    "Тестовете ще минават след поправката."])
def test_questions_and_quoted_red_output_are_not_green_claims(text):
    from genesis_agent import claim_check
    assert claim_check.unsupported_claims(text, _RED) == []


# Ревю 2026-10-11: по-широкият шаблон четеше честния червен отчет и въпросите
# като зелено — подкана, „⚠ червено“ и exit 1. Всички се хващаха преди поправката.
@pytest.mark.parametrize("text", [
    "Пуснах pytest: 4 теста минаха, 1 падна.", "4 теста минават, а test_vat пада.",
    "Старите тестове минават, но новият test_vat пада.", "4 passed and 1 failed",
    "Резултат: 4 passed, 1 xfailed, 2 failed", "4 passed, 1 error in 0.31s",
    "Провери дали всички 5 теста минават.", "Провери дали 5 теста минават.",
    "Тестовете минават ли сега?", "Искаш ли да проверя, че тестовете минават?",
    "Do all 5 tests pass?", "Не всички 5 теста минават.", "Not all tests pass.",
    # Второ ревю 2026-10-11: броените и смесените форми са честен отчет.
    "4 теста минаха, но test_vat не мина.", "Пуснах pytest: 4 теста минаха, 1 не мина.",
    "4 теста минават; test_vat пада.", "4 теста минаха. test_vat още е червен.",
    "Старите тестове минаха, новият е червен.", "3 от 5 теста минават.",
    "Само 3 теста минават.", "Половината тестове минават.",
    "Тестовете минават, освен test_vat.", "Два теста минават, един не.",
    "Тестът минава.", "5 passed in 0.12s"])
def test_honest_mixed_reports_and_questions_are_not_green_claims(text):
    from genesis_agent import claim_check
    assert claim_check.unsupported_claims(text, _RED) == []


@pytest.mark.parametrize("text", [
    "Поправих грешката; всички тестове минават.", "Проверих, че тестовете минават.",
    "Не беше лесно, но всички тестове минаха.", "Fixed the error. All tests pass now."])
def test_a_green_claim_next_to_other_words_is_still_caught(text):
    from genesis_agent import claim_check
    assert claim_check.unsupported_claims(text, _RED)


def test_headless_accepts_an_honest_red_report(ws, monkeypatch, capsys):
    from genesis_agent import headless
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))
    monkeypatch.setattr(headless, "_read_stdin", lambda: "")
    red = "[RUN_CMD: python -m pytest -q]  (rc=1)\n....F\n1 failed, 4 passed"
    real = gs.dispatch_tool_call
    monkeypatch.setattr(gs, "dispatch_tool_call",
                        lambda name, args: red if name == "RUN_CMD" else real(name, args))
    answer = "Пуснах pytest: 4 теста минаха, 1 падна (test_vat). Не успях да го оправя."
    seen = _script(monkeypatch, [
        ("", [_call("RUN_CMD", {"command": "python -m pytest -q"}, "t1")]),
        (answer, None), ("(подкана — не бива)", None)])
    code = headless.run(["--json", "--cwd", str(ws), "оправи тестовете"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"] is True and out["warnings"] == []
    assert len(seen) == 2 and out["result"] == answer


def test_headless_fails_when_the_answer_insists_on_green_after_red(ws, monkeypatch, capsys):
    from genesis_agent import headless
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("SYS", ""))
    monkeypatch.setattr(headless, "_read_stdin", lambda: "")
    red = "[RUN_CMD: python -m pytest -q]  (rc=1)\n..F\n1 failed, 2 passed"
    real = gs.dispatch_tool_call
    monkeypatch.setattr(gs, "dispatch_tool_call",
                        lambda name, args: red if name == "RUN_CMD" else real(name, args))
    claim = "Готово! Всички 3 теста минават."
    _script(monkeypatch, [("", [_call("RUN_CMD", {"command": "python -m pytest -q"}, "t1")]),
                          (claim, None), (claim, None), (claim, None)])
    code = headless.run(["--json", "--cwd", str(ws), "оправи тестовете"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["ok"] is False
    assert "червено" in (out["error"] or "")


# ── F7: /commands показва само каквото наистина тръгва ──────────────────────

_SHADOWED = ("задачи", "умения", "предай", "ъпдейт", "макскод", "готово",
             "локален_макс", "локален_нормал")


def test_custom_commands_named_like_a_cyrillic_builtin_are_not_listed(tmp_path):
    from genesis_agent import chat_commands
    cmds = tmp_path / "ws" / ".genesis" / "commands"
    cmds.mkdir(parents=True)
    (tmp_path / "ws" / ".git").mkdir()
    for name in (*_SHADOWED, "dropbox"):
        (cmds / f"{name}.md").write_text(f"Команда {name} $ARGUMENTS", encoding="utf-8")
    assert sorted(chat_commands.custom_commands(tmp_path / "ws")) == ["dropbox"]


def test_a_custom_command_starting_with_drop_runs(ws, monkeypatch, capsys):
    from genesis_agent import brain, chat_input, model_check, workspace_memory
    cmds = ws / ".genesis" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "dropbox.md").write_text("Качи $ARGUMENTS в Dropbox.", encoding="utf-8")
    monkeypatch.setattr(brain, "any_model_reachable", lambda: True, raising=False)
    monkeypatch.setattr(gta, "print_minimal_banner", lambda: None)
    monkeypatch.setattr(gta, "show_status_bar", lambda: None)
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("system", ""))
    monkeypatch.setattr(model_check, "needs_check", lambda: False)
    monkeypatch.setattr(workspace_memory, "auto_capture", lambda convo: {})
    closed: list = []
    monkeypatch.setattr(workspace_memory, "close_thread",
                        lambda ident, drop=False: closed.append((ident, drop)) or "ok")
    lines = iter(["/dropbox x.txt", "/drop 3", "exit"])
    monkeypatch.setattr(chat_input, "read_message", lambda read: next(lines))
    turns: list[str] = []
    monkeypatch.setattr(gta, "run_turn", lambda messages, text, ui: turns.append(text) or messages)
    monkeypatch.setattr(gta.sys, "argv", ["genesis"])
    gta.main()
    assert turns == ["Качи x.txt в Dropbox."]
    assert closed == [("3", True)]


# ── F6: отговорът на текстов ASK_USER продължава задачата, не е лек въпрос ──

@pytest.mark.parametrize("native", [False, True], ids=["text-tag", "native"])
def test_the_answer_to_ask_user_goes_back_to_the_strong_model(ws, monkeypatch, native):
    from genesis_agent import model_router
    monkeypatch.delenv("GENESIS_CHAT_ROUTING", raising=False)
    question = "В какъв формат да е отчетът — CSV или JSON?"
    first = (("", [_call("ASK_USER", {"question": question}, "q1")]) if native
             else (f"[ASK_USER: {question}]", None))
    _script(monkeypatch, [first])
    ui = _UI()
    msgs = gta.run_turn(_fresh(), "направи report.py, който пише отчета", ui)
    assert any(e[0] == "asked" and question in e[1] for e in ui.log)
    assert model_router.is_light_request("JSON")       # сам по себе си — лек
    assert model_router.is_light_turn(list(msgs) + [{"role": "user", "content": "JSON"}]) is False
