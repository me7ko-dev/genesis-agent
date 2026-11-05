"""Одит 2026-10-31 (шести кръг): /context (genesis_agent.context_usage) и
размерът на последната заявка. Разговорът се брои по копието, което Brain
праща (свитите стари изходи и махнатите стари картинки), а не по пълната
история; снимките от VIEW_IMAGE и проверката в браузър са изход на
инструмент, не реплика на оператора; „по доставчика“ е само числото, което
доставчикът е върнал, и изчезва, щом историята е сменена (/compact, /history,
телефонът). Всеки тест падаше срещу кода преди поправката (възпроизведено
със скриптовете на одита, audit6/context_usage-a и -b)."""
from __future__ import annotations

import json
import struct
import time
import types
import zlib
from collections import deque
from pathlib import Path

import pytest
import requests

import genesis_terminal_agent as gta
from genesis_agent import brain as brain_mod
from genesis_agent import chat_commands, context_usage, vision
from genesis_agent.brain import Brain
from genesis_agent.budget import clip_for_context

SYSTEM = "Ти си Genesis.\n" + "правило\n" * 400


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "x",
                                             "parameters": {"type": "object", "properties": {}}}}


def _round(msgs, name: str, results: list[str]) -> None:
    ids = [f"{name}{i}_{len(msgs)}" for i in range(len(results))]
    msgs.append({"role": "assistant", "content": None, "tool_calls": [
        {"id": c, "type": "function", "function": {"name": name, "arguments": "{}"}} for c in ids]})
    for c, r in zip(ids, results):
        msgs.append({"role": "tool", "tool_call_id": c, "name": name, "content": clip_for_context(r)})


def _tool_heavy_turn() -> deque:
    """Обичаен ход с groq: 10 READ_FILE по ~7.9K знака (таванът на READ_FILE
    без offset/limit) и един RUN_CMD от 6000 (таванът на RUN_CMD) — 16
    съобщения, тоест без автоматична компресия в края на хода."""
    msgs: deque = deque([{"role": "system", "content": SYSTEM}], maxlen=gta._HISTORY_MAXLEN)
    msgs.append({"role": "user", "content": "оправи падащите тестове"})
    _round(msgs, "READ_FILE", [f"# файл {i}\n" + "ред от кода\n" * 660 for i in range(6)])
    _round(msgs, "RUN_CMD", ["FAILED tests/test_x.py::test_y - AssertionError\n" * 125])
    _round(msgs, "READ_FILE", [f"# файл б{i}\n" + "ред от кода\n" * 660 for i in range(4)])
    msgs.append({"role": "assistant", "content": "Поправих двата теста."})
    assert len(msgs) - 1 <= gta._COMPACT_THRESHOLD
    return msgs


def _png() -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _screenshots_turns(turns: int = 2) -> deque:
    """Операторът пише по едно съобщение на ход, моделът вика VIEW_IMAGE за
    8 снимки (MAX_VIEWS_PER_TURN) — те идват като прикачено user съобщение."""
    msgs: deque = deque([{"role": "system", "content": "Ти си Genesis."}], maxlen=gta._HISTORY_MAXLEN)
    for turn in range(turns):
        msgs.append({"role": "user", "content": f"провери страница {turn}"})
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"v{turn}", "type": "function", "function": {"name": "VIEW_IMAGE", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"v{turn}", "name": "VIEW_IMAGE",
                     "content": "8 картинки → следващата заявка"})
        pics = [vision.image_part(_png(), "image/png", f"shot{turn}_{i}.png")
                for i in range(vision.MAX_VIEWS_PER_TURN)]
        msgs.append(vision.attachment_message(pics, "VIEW_IMAGE: " + ", ".join(
            vision.image_name(p) for p in pics)))
        msgs.append({"role": "assistant", "content": "Изглежда добре."})
    return msgs


def _context(messages, monkeypatch, tmp_path, **info) -> str:
    printed: list[str] = []
    chat_commands.handle("/context", messages=messages, workspace=tmp_path, out=printed.append,
                         ask=lambda q: "", context=lambda: {**gta.context_info(), **info})
    return printed[0]


# ── a1. /context броеше пълната история, а Brain праща свито копие ─────────────

def test_context_counts_the_copy_brain_sends_not_the_stored_history(monkeypatch, tmp_path):
    """Обичаен ход с groq и прозорец 32K: /context казваше 74%, „Над 60% —
    /compact“, а Brain.complete праща ~35% — budget_history свива всеки изход
    освен двата най-нови до 2000 знака. /compact би хвърлил файловете за
    резюме без нужда."""
    monkeypatch.setattr(gta, "current_provider", "groq")
    text = _context(_tool_heavy_turn(), monkeypatch, tmp_path, window=32768)
    assert "/compact" not in text, text
    pct = int(text.split("(", 1)[1].split("%", 1)[0])
    assert pct < 50, text
    # Пълната история не изчезва от изгледа — само е назована като история.
    assert "в историята ~21.6K" in text, text


def test_the_counted_conversation_is_the_one_brain_complete_sends(monkeypatch, tmp_path):
    """Изгледът на /context е същият, който истинският Brain.complete дава
    на _call: свит за groq, само допълван за доставчик с кеш по префикс
    (anthropic, ollama_cloud), със старите картинки махнати за всички."""
    sent: dict[str, list] = {}

    def fake_call(self, prov, model, messages, tools=None):
        sent[prov] = messages
        self._last_usage = None
        return "ок", None
    monkeypatch.setattr(Brain, "_call", fake_call)
    monkeypatch.setattr(Brain, "_record_stat", lambda *a: None)
    monkeypatch.setattr(vision, "can_see", lambda prov, model: True)
    tools = [_tool("READ_FILE")]
    for history in (_tool_heavy_turn(), _screenshots_turns(4)):
        for prov in ("groq", "anthropic", "ollama_cloud"):
            b = Brain(use_local=False)
            b.chain = [{"provider": prov, "model": "m", "supports_tools": True}]
            b.local = None
            b._pinned = None
            b.complete(list(history), tools=tools)
            line = next(ln for ln in context_usage.report(sent[prov], tools).splitlines()
                        if "Разговор" in ln)
            counted = context_usage.report(history, tools, provider=prov, model="m")
            assert line in counted.splitlines(), (prov, line, counted)


def test_old_screenshots_brain_leaves_out_are_not_counted(monkeypatch, tmp_path):
    """Два хода по 8 снимки: в историята са 16, Brain праща 8
    (MAX_IMAGES_PER_REQUEST) — /context броеше и 16-те по ~1.5K токена."""
    monkeypatch.setattr(gta, "current_provider", "groq")
    text = _context(_screenshots_turns(2), monkeypatch, tmp_path, window=128000)
    talk = next(ln for ln in text.splitlines() if "Разговор" in ln)
    # 8 снимки по ~1.5K + бележките на махнатите 8; пълното — като история.
    assert talk.split()[1] == "~12.3K", text
    assert "в историята ~24.1K" in text, text


# ── a2. Снимките от VIEW_IMAGE не са реплики на оператора ──────────────────────

def test_view_image_and_page_check_attachments_are_tool_output():
    msgs = _screenshots_turns(2)
    msgs.append({"role": "user", "content": "а сега?"})
    msgs.append(vision.attachment_message([vision.image_part(_png(), "image/png", "page.png")],
                                          "снимки на страницата от проверката в браузър"))
    text = context_usage.report(msgs, [], window=128000)
    # Трите реплики са 43 знака; снимките (3 прикачвания) — изходи на инструменти.
    assert "твоите съобщения 3 ~10," in text, text
    assert "изходи на инструменти 5 " in text, text
    assert "най-голям: VIEW_IMAGE ~12.0K" in text, text
    only_page = context_usage.report(list(msgs)[:1] + list(msgs)[-1:], [])
    assert "най-голям: снимки на страницата от проверката в…" in only_page, only_page


# ── a5 и b2: „Последната заявка по доставчика“ ────────────────────────────────

@pytest.fixture()
def chat(monkeypatch, tmp_path):
    """Истинският gta.main(); входът идва от списък, а ходът към модела само
    отчита една заявка с usage от доставчика (31K) и допълва историята."""
    from genesis_agent import brain, chat_input, model_check, workspace_memory
    ws = tmp_path / "proj"
    ws.mkdir()
    monkeypatch.chdir(ws)
    monkeypatch.setattr(gta, "WORKSPACE", ws)
    monkeypatch.setattr(brain, "any_model_reachable", lambda: True, raising=False)
    monkeypatch.setattr(gta, "print_minimal_banner", lambda: None)
    monkeypatch.setattr(gta, "show_status_bar", lambda: None)
    monkeypatch.setattr(gta, "build_system_prompt", lambda: ("system", ""))
    monkeypatch.setattr(gta, "provider_ready", lambda pk: (True, ""))
    monkeypatch.setattr(model_check, "needs_check", lambda: False)
    monkeypatch.setattr(workspace_memory, "auto_capture", lambda convo: {})
    monkeypatch.setattr(gta.sys, "argv", ["genesis"])
    monkeypatch.setattr(gta, "VOICE_ON", False)
    for name in ("total_input_tokens", "total_output_tokens", "last_context_tokens"):
        monkeypatch.setattr(gta, name, getattr(gta, name))
    monkeypatch.setattr(gta, "last_provider_tokens", 0, raising=False)
    printed: list[str] = []
    monkeypatch.setattr(gta.console, "print", lambda *a, **k: printed.append(" ".join(map(str, a))))
    script: list[str] = []

    def take(_prompt: str = "") -> str:
        if not script:
            raise EOFError
        return script.pop(0)

    monkeypatch.setattr(chat_input, "read_message", lambda read: take())
    monkeypatch.setattr(gta.console, "input", take)

    def run_turn(messages, text, ui, **kw):
        messages.append({"role": "user", "content": text})
        for i in range(6):
            messages.append({"role": "assistant", "content": f"стъпка {i} " + "д" * 4000})
        gta.count_usage({"prompt_tokens": 30600, "completion_tokens": 400}, messages, "ок")
        return messages

    monkeypatch.setattr(gta, "run_turn", run_turn)
    monkeypatch.setattr(gta, "_force_compact", lambda messages: deque(
        [messages[0], {"role": "system", "content": "## Резюме на по-ранния разговор:\nок"}],
        maxlen=gta._HISTORY_MAXLEN))

    def drive(*lines: str) -> str:
        printed.clear()
        script[:] = list(lines)
        gta.main()
        return "\n".join(printed)

    return types.SimpleNamespace(drive=drive, ws=ws)


def _older_conversation() -> Path:
    old = gta.HISTORY_DIR / "session_20200101_000000.json"
    old.write_text(json.dumps([{"role": "system", "content": "x"},
                               {"role": "user", "content": "стар разговор"},
                               {"role": "assistant", "content": "ок"}], ensure_ascii=False),
                   encoding="utf-8")
    return old


@pytest.mark.parametrize("command", [["/compact"], ["/history", "1"]])
def test_replacing_the_history_forgets_the_old_request_size(chat, command):
    """След /compact /context показваше новата оценка (~1K) до „Последната
    заявка по доставчика: 31.0K“ на разговора, който вече го няма, а статус
    редът — „ctx 31K/131K (23%)“ до следващата заявка. Σ остава."""
    if command[0] == "/history":
        _older_conversation()
    printed = chat.drive("направи нещо", "/context", *command, "/context")
    # Първият /context е след истинска заявка — числото на доставчика е там.
    shown = [ln for ln in printed.splitlines() if "по доставчика" in ln]
    assert shown == ["Последната заявка по доставчика: 31.0K токена (с отговора)"], printed
    assert "по доставчика" not in printed.rsplit("Контекст:", 1)[1], printed
    # Статус редът — оценката на новата история, не 0 (прегледът, 2026-11-05).
    assert 0 < gta.get_context_stats()[0] < 1000
    assert gta.total_input_tokens + gta.total_output_tokens == 31000


def test_a_reply_without_provider_usage_is_not_shown_as_the_providers_count(monkeypatch, tmp_path):
    """Доставчик без `usage` (локален Ollama /v1 понякога): count_usage
    слагаше собствената си оценка — само съдържанието, без схемите и
    аргументите на tool_calls — и /context я показваше като число „по
    доставчика“, по-малко от собствената си оценка (1.8K срещу 8.8K)."""
    for name in ("total_input_tokens", "total_output_tokens", "last_context_tokens"):
        monkeypatch.setattr(gta, name, 0)
    monkeypatch.setattr(gta, "last_provider_tokens", 0, raising=False)
    monkeypatch.setattr(gta, "current_provider", "groq")
    monkeypatch.delenv("GENESIS_LOCAL_ONLY", raising=False)

    class FakeBrain:
        current = None

        def __init__(self, *a, **k) -> None:
            pass

        def complete(self, messages, tools=None):
            return types.SimpleNamespace(raw_text="Записах app.py.", usage=None, tool_calls=None)
    monkeypatch.setattr(brain_mod, "Brain", FakeBrain)
    msgs = deque([{"role": "system", "content": SYSTEM},
                  {"role": "user", "content": "напиши app.py"},
                  {"role": "assistant", "content": None, "tool_calls": [
                      {"id": "w", "type": "function", "function": {
                          "name": "WRITE_FILE",
                          "arguments": json.dumps({"path": "app.py", "content": "x = 1\n" * 4000})}}]},
                  {"role": "tool", "tool_call_id": "w", "name": "WRITE_FILE", "content": "✓ записан"}],
                 maxlen=gta._HISTORY_MAXLEN)
    gta.ask_genesis(msgs, tools=[_tool("WRITE_FILE")])
    assert gta.last_context_tokens > 0   # статус редът пази оценката си
    text = _context(msgs, monkeypatch, tmp_path)
    assert "по доставчика" not in text, text


def test_a_legacy_call_that_never_reached_ollama_is_not_a_request(monkeypatch, tmp_path):
    """Ollama не слуша: call_ollama връща „[Ollama не отговаря: …]“ като низ и
    _ask_via_legacy пак викаше count_usage — Σ растеше с ~2K токена, които
    никой не е похарчил, а /context показваше заявка „по доставчика“."""
    for name in ("total_input_tokens", "total_output_tokens", "last_context_tokens"):
        monkeypatch.setattr(gta, name, 0)
    monkeypatch.setattr(gta, "last_provider_tokens", 0, raising=False)
    monkeypatch.setattr(gta, "current_provider", "ollama")
    monkeypatch.setattr(gta, "current_model_id", "llama3.2")
    monkeypatch.delenv("GENESIS_LOCAL_ONLY", raising=False)

    def refused(*a, **k):
        raise requests.exceptions.ConnectionError("Connection refused")
    monkeypatch.setattr(gta.requests, "post", refused)
    msgs = deque([{"role": "system", "content": SYSTEM}, {"role": "user", "content": "здрасти"}],
                 maxlen=gta._HISTORY_MAXLEN)
    reply, _ = gta.ask_genesis(msgs, tools=None)
    assert reply.startswith("[Ollama не отговаря")
    assert (gta.total_input_tokens, gta.total_output_tokens) == (0, 0)
    assert gta.context_info()["last"] == 0
    assert "по доставчика" not in _context(msgs, monkeypatch, tmp_path)


# ── a5 от телефона: runner-ът на `genesis serve` ──────────────────────────────

def _wait(session, timeout: float = 15) -> None:
    deadline = time.time() + timeout
    time.sleep(0.05)
    while session.busy and time.time() < deadline:
        time.sleep(0.02)


def test_compact_from_the_phone_forgets_the_old_request_size(chat, monkeypatch):
    pytest.importorskip("cryptography")
    from genesis_agent import remote_server as rs
    monkeypatch.setattr(rs, "_print_qr", lambda *a: None)
    cap: dict = {}
    real_init = rs.RemoteServer.__init__

    def init(self, *a, **k):
        real_init(self, *a, **k)
        cap["server"] = self
    seen: dict = {}

    class FakeHTTP:
        def serve_forever(self, poll_interval=0.5):
            session = cap["server"].session
            session.send("направи нещо")
            _wait(session)
            seen["before"] = gta.context_info()["last"]
            session.send("/compact")
            _wait(session)
            seen["after"] = (gta.context_info()["last"], gta.get_context_stats()[0])
            raise KeyboardInterrupt

        def server_close(self):
            pass
    monkeypatch.setattr(rs.RemoteServer, "__init__", init)
    monkeypatch.setattr(rs.RemoteServer, "make_http", lambda self, h, p: FakeHTTP())
    assert rs.serve(["--port", "0"]) == 0
    assert seen["before"] == 31000
    assert seen["after"][0] == 0 and 0 < seen["after"][1] < 1000, seen


def test_loading_a_conversation_on_the_desktop_forgets_the_old_request_size(monkeypatch):
    """„История“ в Desktop/телефона (desktop_commands.load_history) сменя
    разговора като /resume — числото на заявката оставаше от стария."""
    from genesis_agent import desktop_commands as dc
    for name in ("total_input_tokens", "total_output_tokens", "last_context_tokens"):
        monkeypatch.setattr(gta, name, 0)
    monkeypatch.setattr(gta, "last_provider_tokens", 0, raising=False)
    old = _older_conversation()
    gta.count_usage({"prompt_tokens": 30600, "completion_tokens": 400}, [], "ок")
    state = {"m": [{"role": "system", "content": "SYS"}]}
    ctx = dc.CommandContext(get_messages=lambda: state["m"], set_messages=lambda m: state.update(m=m),
                            busy=lambda: False, emit=lambda k, **d: None)
    assert dc.run("load_history", {"file": old.name}, ctx)["ok"]
    assert gta.context_info()["last"] == 0
    assert gta.get_context_stats()[0] == gta._history_tokens(state["m"])
    assert gta.total_input_tokens == 30600


# ── Прегледът (2026-11-05): автоматичната компресия и /undo ──


def test_undo_keeps_the_status_bar_on_the_same_conversation(chat, monkeypatch):
    """/undo само добавя бележка към същия разговор, а статус редът ставаше
    „ctx 0/131K (0%)“ до следващата заявка — забравянето го нулираше."""
    from genesis_agent import edit_history
    monkeypatch.setattr(edit_history, "pending", lambda: ["app.py"])
    monkeypatch.setattr(edit_history, "undo", lambda: ("направи нещо", ["app.py"]))
    printed = chat.drive("направи нещо", "/undo", "")
    assert "Върнат ходът" in printed, printed
    assert gta.context_info()["last"] == 0
    assert gta.get_context_stats()[0] > 5000


def test_the_automatic_compaction_after_a_turn_forgets_the_old_request(tmp_path, monkeypatch):
    """Компресията в края на хода (над 16 съобщения) сменя историята като
    /compact, но „по доставчика“ оставаше на заявката отпреди нея."""
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    import genesis_skills as gs
    from genesis_agent import hooks
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    monkeypatch.setattr(gta, "WORKSPACE", root)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    monkeypatch.setattr(hooks, "fire", lambda *a, **k: hooks.Outcome())
    for name in ("total_input_tokens", "total_output_tokens", "last_context_tokens"):
        monkeypatch.setattr(gta, name, 0)
    monkeypatch.setattr(gta, "last_provider_tokens", 0, raising=False)

    def ask(messages, tools=None):
        gta.count_usage({"prompt_tokens": 30600, "completion_tokens": 400}, messages, "ок")
        return "ок", None
    monkeypatch.setattr(gta, "ask_genesis", ask)
    short = deque([{"role": "system", "content": "SYS"},
                   {"role": "system", "content": "## Резюме на по-ранния разговор:\nок"}],
                  maxlen=gta._HISTORY_MAXLEN)
    monkeypatch.setattr(gta, "_compact_messages", lambda messages: short)

    class UI(gta.TurnUI):
        def __getattr__(self, name):
            return lambda *a, **k: None

    try:
        out = gta.run_turn(deque([{"role": "system", "content": "SYS"}],
                                 maxlen=gta._HISTORY_MAXLEN), "здрасти", UI())
    finally:
        gs._set_agent_scope(None)
        gs._set_cancel(None)
        hooks.workspace = None
    assert any("Резюме" in str(m.get("content")) for m in out), list(out)
    assert gta.context_info()["last"] == 0
    assert gta.get_context_stats()[0] == gta._history_tokens(short)
    assert gta.total_input_tokens == 30600
