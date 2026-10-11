"""E2E проверката 2026-10-11 (вълна 2): отрязан на тавана ход, пазачът без
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


# ── F1: всеки опит отрязан на тавана → моделът научава и пише на части ──────

_CUT = ("[Грешка: HTTP_TRUNCATED_ALL: всеки изпратен опит е отрязан на тавана на "
        "изходните токени | последна: HTTP_TRUNCATED: …]")


def test_a_cut_turn_tells_the_model_to_write_in_parts(ws, monkeypatch):
    seen = _script(monkeypatch, [
        (_CUT, None),
        ("", [_call("WRITE_FILE", {"path": "big.md", "content": "# x\n"}, "w1")]),
        ("Записах big.md.", None)])
    ui = _UI()
    msgs = gta.run_turn(_fresh(), "направи big.md с 50 реда", ui)
    assert len(seen) == 3
    assert "отрязан" in str(seen[1][-1]["content"]) and seen[1][-1]["role"] == "system"
    assert (ws / "big.md").exists()
    assert not any(m["role"] == "assistant" and str(m.get("content")).startswith("[Грешка")
                   for m in msgs)


def test_a_cut_turn_asks_only_once(ws, monkeypatch):
    seen = _script(monkeypatch, [(_CUT, None), (_CUT, None), (_CUT, None)])
    ui = _UI()
    msgs = gta.run_turn(_fresh(), "направи big.py", ui)
    assert len(seen) == 2
    assert ("warn", _CUT) in ui.log
    # Подканата остава в историята — следващото „продължи“ я вижда.
    assert any(m["role"] == "system" and "отрязан" in str(m["content"]) for m in msgs)


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
