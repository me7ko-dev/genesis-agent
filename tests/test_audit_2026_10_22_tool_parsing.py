"""Одит на разбора на инструментите, 2026-10-22: от отговора на модела до
извикване. Всеки тест пада без поправката си (пуснато срещу стария код).

1. Таг в `код` или в ``` ограда (пример, цитат на вече пуснатото) се изпълняваше.
2. WRITE_FILE в текстов режим, отрязан на тавана, получаваше „грешен синтаксис“.
3. Anthropic: tool_use, отрязан на max_tokens, се изпълняваше с половината вход.
4. `[ERROR]` / `[INFO]` във финален отговор пращаха подканата за синтаксис.
5. <invoke> XML: `]` в командата я режеше, `[` губеше извикването, незатворен
   <invoke> поглъщаше следващия.
6. Текстов EDIT_FILE с CRLF не намираше котвата.
"""
from __future__ import annotations

import contextlib
import json
import sys
import types
from collections import deque

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import brain, hooks


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


def _script(monkeypatch, replies):
    """Фалшив модел: връща отговорите по ред и пази какво е видял всеки път."""
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


def _lines(path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


# ── 1. таг, показан като пример или цитат, не се изпълнява ────────────────────

def test_a_tag_shown_in_backticks_or_a_fence_is_text_not_a_call(ws, monkeypatch):
    log = ws / "log.txt"
    # Пример в обяснение: „Genesis би изпълнил `[RUN_CMD: git stash]` … не пускам нищо“.
    out = gs.parse_and_execute_tools(
        "За да добавиш ред, Genesis би изпълнил `[RUN_CMD: echo line >> log.txt]`. "
        "Не пускам нищо — кажи, ако искаш.")
    assert out == [] and not log.exists()
    # Пример в ``` ограда: и WRITE_FILE, и RUN_CMD остават текст.
    out = gs.parse_and_execute_tools(
        "Синтаксисът е:\n```\n[WRITE_FILE: hello.txt]\nпример\n[END_WRITE]\n```\n"
        "```bash\n[RUN_CMD: echo line >> log.txt]\n```\nНе съм пускал нищо.")
    assert out == [] and not (ws / "hello.txt").exists() and not log.exists()
    # Истинските тагове до тях тръгват; ``` в ТЯЛОТО на WRITE_FILE е съдържание
    # и не скрива таговете след него (нечетен брой огради в README).
    out = gs.parse_and_execute_tools(
        "[WRITE_FILE: README.md]\n# Пример\n```bash\npip install x\n[END_WRITE]\n"
        "Примерът е `[RUN_CMD: echo no >> log.txt]`, а пускам:\n[RUN_CMD: echo yes >> log.txt]")
    assert len(out) == 2 and (ws / "README.md").exists()
    assert [ln.strip() for ln in _lines(log)] == ["yes"]
    log.unlink()

    # Финалното резюме цитира вече пуснатата команда — тя не тръгва втори път.
    _script(monkeypatch, [
        ("Добавям ред.\n[RUN_CMD: echo line >> log.txt]", None),
        ("Готово — командата `[RUN_CMD: echo line >> log.txt]` добави ред в log.txt.", None),
        ("Готово.", None), ("Готово.", None),
    ])
    ui = _UI()
    gta.run_turn(_fresh(), "добави ред в log.txt", ui)
    assert len(_lines(log)) == 1, _lines(log)
    assert len([e for e in ui.log if e[0] == "tool" and e[1] == "RUN_CMD"]) == 1


def test_a_quoted_block_tag_does_not_swallow_the_real_one_after_it(ws):
    """Ревю 2026-10-22: `[WRITE_FILE: път]` в `код` стигаше до [END_WRITE] на
    истинския запис след него — примерът се прескача, а истинският не тръгваше."""
    out = gs.parse_and_execute_tools(
        "Ще ползвам `[WRITE_FILE: път]`, за да запиша файла:\n"
        "[WRITE_FILE: a.py]\nx = 1\n[END_WRITE]")
    assert len(out) == 1 and (ws / "a.py").read_text(encoding="utf-8") == "x = 1\n", out
    assert not (ws / "път").exists()
    # Същото за EDIT_FILE, цитиран в тялото на README преди истинската редакция.
    (ws / "app.py").write_text("X = 1\n", encoding="utf-8")
    gs.parse_and_execute_tools("[READ_FILE: app.py]")
    out = gs.parse_and_execute_tools(
        "[WRITE_FILE: README.md]\nРедакция: `[EDIT_FILE: път]`.\n[END_WRITE]\n"
        "[EDIT_FILE: app.py]\nX = 1\n---GENESIS-REPLACE-WITH---\nX = 2\n[END_EDIT]")
    assert len(out) == 2 and (ws / "app.py").read_text(encoding="utf-8") == "X = 2\n", out
    assert "[EDIT_FILE: път]" in (ws / "README.md").read_text(encoding="utf-8")


# ── 2. текстов WRITE_FILE, отрязан на изходния таван ──────────────────────────

_CUT = ("Пиша страницата.\n[WRITE_FILE: index.html]\n<!doctype html>\n<html><body>\n"
        "<h1>Каталог</h1>\n<table>\n<tr><td>ред 1</td></tr>\n<tr><td>ре")


def test_a_text_write_cut_at_the_output_cap_is_reported_as_cut(ws, monkeypatch):
    class _Resp:
        status_code, headers = 200, {}
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": _CUT}, "finish_reason": "length"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4096}}

    monkeypatch.setattr(brain, "_post_with_deadline", lambda *a, **kw: _Resp())
    b = brain.Brain.__new__(brain.Brain)
    with pytest.raises(RuntimeError, match="HTTP_TRUNCATED"):
        b._http("https://x/v1", "k", "m", [{"role": "user", "content": "x"}], 10)
    # Отрязаният текст се пази като при native пътя: ако го отреже всеки модел,
    # стига до цикъла вместо „Error“.
    assert b._cut_reply and b._cut_reply[0].endswith("<tr><td>ре")
    # Затворен WRITE_FILE на тавана си е цял — той минава.
    assert not brain._is_truncated("[WRITE_FILE: a.py]\nx = 1\n[END_WRITE]")
    assert not brain._is_truncated("[USE_SKILL: csv]")       # валидната кратка форма

    # Цикълът: моделът научава, че записът е отрязан, а не че синтаксисът е грешен.
    seen = _script(monkeypatch, [(_CUT, None), (_CUT, None), (_CUT, None), ("Готово.", None)])
    gta.run_turn(_fresh(), "направи index.html с каталог", _UI())
    notes = [m["content"] for m in seen[1] if m.get("role") == "system" and m["content"] != "SYS"]
    assert notes, "моделът не получи никаква бележка"
    assert "отрязан" in notes[-1] and "Раздели" in notes[-1], notes[-1]
    assert "не намерих валиден tool таг" not in notes[-1]
    # Таг, само споменат в `код` във финалното резюме, не е отрязан запис (ревю
    # 2026-10-22): „раздели записа“ би пратило модела да пише файла пак.
    note = gs.malformed_tag_note("Готово — създадох файла с `[WRITE_FILE: app.py]`.")
    assert "Раздели" not in note and "не се изпълнява" in note, note


# ── 3. Anthropic: tool_use, отрязан на max_tokens ─────────────────────────────

def _fake_anthropic(monkeypatch, reply) -> None:
    class _Messages:
        def stream(self, **params):
            return contextlib.nullcontext(types.SimpleNamespace(get_final_message=lambda: reply))

    fake = types.ModuleType("anthropic")
    for name in ("BadRequestError", "AuthenticationError", "PermissionDeniedError",
                 "RateLimitError", "APIStatusError", "APIConnectionError"):
        setattr(fake, name, type(name, (Exception,), {"status_code": 500}))
    fake.Anthropic = lambda **_kw: types.SimpleNamespace(
        beta=types.SimpleNamespace(messages=_Messages()), messages=_Messages())
    monkeypatch.setitem(sys.modules, "anthropic", fake)


def test_an_anthropic_tool_use_cut_at_max_tokens_is_not_run_with_half_its_input(ws, monkeypatch):
    usage = types.SimpleNamespace(input_tokens=10, output_tokens=64000,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0)
    # SDK-то пълни input от частичния JSON (partial_mode) — валиден, но отрязан dict.
    cut = types.SimpleNamespace(type="tool_use", id="toolu_01", name="WRITE_FILE", input={
        "path": "report.py", "content": "def main():\n    rows = load()\n    for r in ro"})
    _fake_anthropic(monkeypatch, types.SimpleNamespace(
        stop_reason="max_tokens", content=[cut], usage=usage))
    b = brain.Brain()
    tools = [{"type": "function", "function": {"name": "WRITE_FILE", "parameters": {}}}]
    with pytest.raises(RuntimeError, match="HTTP_TRUNCATED"):
        b._call_anthropic("k", "claude-opus-5", [{"role": "user", "content": "напиши report.py"}],
                          tools, "high")
    # Като при OpenAI пътя: отрязаният call се пази за цикъла, но аргументите му
    # не са цял JSON — изпълнението казва „раздели записа“ и не пише нищо.
    assert b._cut_reply is not None
    kept = b._cut_reply[1][0]["function"]
    assert brain._has_cut_tool_arguments(b._cut_reply[1])
    out = gs.dispatch_tool_call(kept["name"], kept["arguments"])
    assert "отрязани" in out and not (ws / "report.py").exists()

    # Цял tool_use на max_tokens (отрязан е текстът след него) минава нормално.
    whole = types.SimpleNamespace(type="tool_use", id="toolu_02", name="READ_FILE",
                                  input={"path": "a.py"})
    tail = types.SimpleNamespace(type="text", text="Чета")
    _fake_anthropic(monkeypatch, types.SimpleNamespace(
        stop_reason="max_tokens", content=[whole, tail], usage=usage))
    _text, calls = brain.Brain()._call_anthropic("k", "claude-opus-5",
                                                 [{"role": "user", "content": "x"}], tools, "high")
    assert calls and json.loads(calls[0]["function"]["arguments"]) == {"path": "a.py"}


# ── 4. обикновени думи в скоби не са „сгрешен таг“ ────────────────────────────

def test_bracketed_log_levels_in_a_final_answer_do_not_get_the_syntax_nudge(ws, monkeypatch):
    answer = ("Логът е наред: всеки неуспешен опит се пише като ред с `[ERROR]` отпред, "
              "а успешният с `[INFO]`.")
    seen = _script(monkeypatch, [(answer, None), ("Готово.", None), ("Готово.", None)])
    gta.run_turn(_fresh(), "какво пише в лога?", _UI())
    assert len(seen) == 1
    assert not gs.looks_like_attempted_tool_tag("Ред [ERROR] и ред [INFO]; ключът е os.environ[PATH].")
    assert not gs.looks_like_attempted_tool_tag("Готово [DONE]")
    # Истинските опити още се хващат: измислен инструмент, грешно име, незатворен блок.
    assert gs.looks_like_attempted_tool_tag("[INSTALL: requests]")
    assert gs.looks_like_attempted_tool_tag("[READFILE] app.py")
    assert gs.looks_like_attempted_tool_tag("[WRITE_FILE: a.py]\nx = 1\n")
    # Познат таг в `код` не се изпълнява — подканата му казва защо.
    assert gs.looks_like_attempted_tool_tag("`[READ_FILE: a.py]`")


# ── 5. <invoke> XML: стойността на параметъра е данни ─────────────────────────

def test_an_xml_call_runs_with_exactly_its_parameter_value(ws):
    out = gs.parse_and_execute_tools('<invoke name="RUN_CMD">python -c "print(\'a]b\')"</invoke>')
    assert len(out) == 1 and "rc=0" in out[0] and "a]b" in out[0], out
    out = gs.parse_and_execute_tools(
        '<invoke name="RUN_CMD"><parameter name="command">python -c "print(\'[\')"'
        '</parameter></invoke>')
    assert len(out) == 1 and "rc=0" in out[0], out
    # Незатворен <invoke> не поглъща следващия: READ_FILE тръгва, а XML-ът му
    # не стига до shell-а.
    (ws / "a.py").write_text("важен код\n", encoding="utf-8")
    out = gs.parse_and_execute_tools(
        'Пускам тестовете и чета файла.\n<invoke name="RUN_CMD">\n'
        '<parameter name="command">python -c "print(1)"\n\n'
        '<invoke name="READ_FILE">\n<parameter name="path">a.py</parameter>\n</invoke>')
    assert any("важен код" in r for r in out), out
    assert not any("<invoke" in r or "Syntax error" in r for r in out), out


def test_an_xml_write_whose_content_holds_an_invoke_is_written_not_run(ws):
    """Ревю 2026-10-22 на поправката горе: `(?!<invoke)` спираше и съдържанието —
    тест с XML пример в content не се записваше, а примерът се ИЗПЪЛНЯВАШЕ;
    само пътят на WRITE_FILE отиваше в слот („\\x000\\x00“, „извън workspace“)."""
    log = ws / "log.txt"
    out = gs.parse_and_execute_tools(
        '<invoke name="WRITE_FILE"><parameter name="path">t.py</parameter>'
        '<parameter name="content">S = \'<invoke name="RUN_CMD">echo no >> log.txt</invoke>\'\n'
        '</parameter></invoke>')
    assert len(out) == 1 and "✓" in out[0], out
    assert (ws / "t.py").exists() and not log.exists()
    (ws / "e.py").write_text("A = 1\n", encoding="utf-8")
    gs.parse_and_execute_tools("[READ_FILE: e.py]")
    gs.parse_and_execute_tools(
        '<invoke name="EDIT_FILE"><parameter name="path">e.py</parameter>'
        '<parameter name="old_string">A = 1</parameter><parameter name="new_string">'
        'A = \'<invoke name="RUN_CMD">echo no >> log.txt</invoke>\'</parameter></invoke>')
    assert "echo no" in (ws / "e.py").read_text(encoding="utf-8") and not log.exists()
    # Отварящ <invoke> само с пътя, тялото до [END_WRITE] — пътят е истинският.
    out = gs.parse_and_execute_tools('<invoke name="WRITE_FILE">w.py</invoke>X = 2\n[END_WRITE]')
    assert (ws / "w.py").read_text(encoding="utf-8") == "X = 2\n", out
    out = gs.parse_and_execute_tools(
        '<invoke name="WRITE_FILE"><parameter name="path">w2.py</parameter></invoke>\n'
        'X = 3\n[END_WRITE]')
    assert (ws / "w2.py").read_text(encoding="utf-8") == "X = 3\n", out
    # `<invoke` в самата команда (grep в този репо) не е второ извикване.
    out = gs.parse_and_execute_tools('<invoke name="RUN_CMD">python -c "print(\'<invoke\')"</invoke>')
    assert len(out) == 1 and "rc=0" in out[0], out


# ── 6. текстов EDIT_FILE с CRLF ───────────────────────────────────────────────

def test_a_text_edit_written_with_crlf_applies_to_an_lf_file(ws):
    app = ws / "app.py"
    app.write_bytes(b"A = 1\nB = 2\nC = 3\n")
    gs.parse_and_execute_tools("[READ_FILE: app.py]")
    out = gs.parse_and_execute_tools(
        "[EDIT_FILE: app.py]\r\nA = 1\r\nB = 2\r\n---GENESIS-REPLACE-WITH---\r\n"
        "A = 10\r\nB = 20\r\n[END_EDIT]")
    assert len(out) == 1 and "❌" not in out[0], out
    assert app.read_bytes() == b"A = 10\nB = 20\nC = 3\n"
