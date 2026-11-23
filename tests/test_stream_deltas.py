"""Поток дума по дума (2026-11-23): Desktop и облакът показваха отговора чак в
края. Сега, когато UI-ят го иска (`delta`), доставчикът се пита поточно и
частите текст стигат до чата, докато пристигат; крайният отговор е същият.

Всеки тест пада без поправката: преди нея нямаше нито `stream_deltas`, нито
събитие "delta", а _http/_anthropic_final питаха само наведнъж."""
from __future__ import annotations

import contextlib
import json
import sys
import time
import types
from collections import deque

import pytest

import genesis_skills as gs
import genesis_terminal_agent as gta
from genesis_agent import brain as brain_mod
from genesis_agent import hooks
from genesis_agent import remote_server as rs
from genesis_agent.brain import Brain, DeltaBatcher, stream_deltas

# ── OpenAI-съвместимите (_http) ────────────────────────────────────────────


def _sse(*chunks: dict) -> list[bytes]:
    lines = [b": OPENROUTER PROCESSING", b""]
    for c in chunks:
        lines += [b"data: " + json.dumps(c, ensure_ascii=False).encode("utf-8"), b""]
    return lines + [b"data: [DONE]", b""]


class _FakeStreamResponse:
    def __init__(self, lines: list[bytes], status: int = 200,
                 ctype: str = "text/event-stream", body: bytes = b"") -> None:
        self.status_code = status
        self.headers = {"content-type": ctype}
        self._lines = lines
        self.content = body
        self.text = body.decode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def iter_lines(self, chunk_size=512, decode_unicode=False):
        yield from self._lines

    def json(self):
        return json.loads(self.content)


TEXT_CHUNKS = ["Здравей", ", ето", " плана:\n", "1. тестове"]


def _text_stream() -> list[bytes]:
    chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}]
    chunks += [{"choices": [{"index": 0, "delta": {"content": t}}]} for t in TEXT_CHUNKS]
    chunks += [{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
               {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 7}}]
    return _sse(*chunks)


@pytest.fixture
def posted(monkeypatch):
    """Подменя requests.post на brain; връща записа на изпратените тела."""
    sent: list[dict] = []
    replies: list[_FakeStreamResponse] = []

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        sent.append({"json": json, "stream": stream})
        return replies.pop(0)
    monkeypatch.setattr(brain_mod.requests, "post", fake_post)
    return sent, replies


def _collect() -> tuple[list[tuple[str, bool]], object]:
    got: list[tuple[str, bool]] = []
    return got, lambda text, start: got.append((text, start))


def test_openai_stream_sends_text_parts_in_order_and_the_same_final_text(posted) -> None:
    sent, replies = posted
    replies.append(_FakeStreamResponse(_text_stream()))
    got, sink = _collect()
    with stream_deltas(sink):
        text, calls = Brain()._http("https://x/v1", "k", "m", [{"role": "user", "content": "x"}], 30)
    assert [t for t, _ in got] == TEXT_CHUNKS
    assert [s for _, s in got] == [True, False, False, False]
    assert "".join(t for t, _ in got) == text == "Здравей, ето плана:\n1. тестове"
    assert calls is None
    assert sent[0]["stream"] is True and sent[0]["json"]["stream"] is True


def test_openai_stream_builds_tool_calls_but_never_shows_their_json(posted) -> None:
    _, replies = posted
    replies.append(_FakeStreamResponse(_sse(
        {"choices": [{"index": 0, "delta": {"content": "Чета файла."}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "c1", "type": "function",
             "function": {"name": "READ_FILE", "arguments": ""}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "{\"path\": "}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "\"a.py\"}"}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    )))
    got, sink = _collect()
    with stream_deltas(sink):
        text, calls = Brain()._http("https://x/v1", "k", "m", [{"role": "user", "content": "x"}], 30,
                                    tools=[{"type": "function", "function": {"name": "READ_FILE"}}])
    assert [t for t, _ in got] == ["Чета файла."]
    assert text == "Чета файла."
    assert calls == [{"id": "c1", "type": "function",
                      "function": {"name": "READ_FILE", "arguments": "{\"path\": \"a.py\"}"}}]


def test_without_a_sink_the_request_is_not_streamed(monkeypatch) -> None:
    """Терминалът (и всичко без UI за потока): заявката е като преди."""
    seen: list[dict] = []

    class _R:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}]}

    def fake(url, *, headers, json, timeout):
        seen.append(json)
        return _R()
    monkeypatch.setattr(brain_mod, "_post_with_deadline", fake)
    text, _ = Brain()._http("https://x/v1", "k", "m", [{"role": "user", "content": "x"}], 30)
    assert text == "готово"
    assert "stream" not in seen[0] and "stream_options" not in seen[0]


def test_a_provider_that_refuses_stream_is_asked_again_without_it(posted, monkeypatch) -> None:
    _, replies = posted
    replies.append(_FakeStreamResponse([], status=400, ctype="application/json",
                                       body=b'{"error": "stream_options is not supported"}'))
    plain: list[dict] = []

    class _R:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "наведнъж"}, "finish_reason": "stop"}]}

    def fake(url, *, headers, json, timeout):
        plain.append(json)
        return _R()
    monkeypatch.setattr(brain_mod, "_post_with_deadline", fake)
    got, sink = _collect()
    with stream_deltas(sink):
        text, _ = Brain()._http("https://x/v1", "k", "m", [{"role": "user", "content": "x"}], 30)
    assert text == "наведнъж" and got == []
    assert "stream" not in plain[0]


def test_an_error_inside_the_stream_goes_to_the_next_model(posted) -> None:
    _, replies = posted
    replies.append(_FakeStreamResponse(_sse(
        {"choices": [{"index": 0, "delta": {"content": "Почвам"}}]},
        {"error": {"message": "upstream", "code": 502}})))
    with stream_deltas(lambda t, s: None), pytest.raises(RuntimeError, match="HTTP_200_MALFORMED"):
        Brain()._http("https://x/v1", "k", "m", [{"role": "user", "content": "x"}], 30)


# ── Anthropic (_anthropic_final) ───────────────────────────────────────────


def _ev(kind: str, **delta) -> types.SimpleNamespace:
    return types.SimpleNamespace(type="content_block_delta", index=0,
                                 delta=types.SimpleNamespace(type=kind, **delta))


class _Final:
    stop_reason = "end_turn"
    usage = None

    def __init__(self, text: str) -> None:
        self.content = [types.SimpleNamespace(type="text", text=text)]


@pytest.fixture
def anthropic_sdk(monkeypatch):
    state = {"iterated": False}
    events = [
        types.SimpleNamespace(type="message_start"),
        _ev("thinking_delta", thinking="тайният размисъл"),
        _ev("text_delta", text="Здравей"),
        types.SimpleNamespace(type="text", text="Здравей"),   # производното на SDK-то
        _ev("text_delta", text=" от Claude"),
        _ev("input_json_delta", partial_json="{\"path\""),
        types.SimpleNamespace(type="message_stop"),
    ]

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

        def __iter__(self):
            state["iterated"] = True
            return iter(events)

        def get_final_message(self):
            return _Final("Здравей от Claude")

    class _Messages:
        def stream(self, **_params):
            return _Stream()

    fake = types.ModuleType("anthropic")
    for name in ("BadRequestError", "AuthenticationError", "PermissionDeniedError",
                 "RateLimitError", "APIStatusError", "APIConnectionError"):
        setattr(fake, name, type(name, (Exception,), {"status_code": 500}))

    class _Client:
        def __init__(self, **_kw) -> None:
            self.beta = types.SimpleNamespace(messages=_Messages())
            self.messages = _Messages()
    fake.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return state


def test_anthropic_streams_only_the_text(anthropic_sdk) -> None:
    got, sink = _collect()
    with stream_deltas(sink):
        text, _ = Brain()._call_anthropic("key", "claude-opus-5",
                                          [{"role": "user", "content": "здрасти"}], None, "")
    assert got == [("Здравей", True), (" от Claude", False)]
    assert "".join(t for t, _ in got) == text


def test_anthropic_without_a_sink_does_not_touch_the_events(anthropic_sdk) -> None:
    text, _ = Brain()._call_anthropic("key", "claude-opus-5",
                                      [{"role": "user", "content": "здрасти"}], None, "")
    assert text == "Здравей от Claude"
    assert anthropic_sdk["iterated"] is False


# ── на порции ──────────────────────────────────────────────────────────────


def test_the_batcher_keeps_order_and_flushes_the_tail() -> None:
    out: list[tuple[str, bool]] = []
    b = DeltaBatcher(lambda t, s: out.append((t, s)), every=0.2)
    for part in ("a", "b", "c"):
        b(part, start=part == "a")
    assert out == [("a", True)]          # първата веднага, останалите чакат
    time.sleep(0.4)
    assert out == [("a", True), ("bc", False)]
    b("x", start=True)                   # нов опит: изтрива чакащото, тръгва веднага
    assert out[-1] == ("x", True)


# ── `genesis serve` ────────────────────────────────────────────────────────


def test_the_session_forwards_deltas_and_drops_them_after_the_answer() -> None:
    seen: list[dict] = []
    session = rs.RemoteSession(lambda t, ui: None, on_event=seen.append)
    session.delta("Здра", start=True)
    session.delta("вей")
    session.emit("progress", text="под-агентът чете")   # не затваря потока
    session.delta("!")
    session.emit("assistant", text="Здравей!")
    session.delta(" закъсняла")                          # след края — нищо
    kinds = [(e["type"], e.get("text"), e.get("start")) for e in seen]
    assert kinds == [("delta", "Здра", True), ("delta", "вей", None),
                     ("progress", "под-агентът чете", None), ("delta", "!", None),
                     ("assistant", "Здравей!", None)]
    # Повторно свързан телефон вижда само крайния текст.
    log = session.events_after(0)["events"]
    assert [e["type"] for e in log] == ["progress", "assistant"]


def test_a_long_stream_does_not_push_the_conversation_out_of_the_log(monkeypatch) -> None:
    """Дълъг отговор — стотици порции: над тавана те се сливат в една със
    "start" (целият текст дотук), а разговорът остава в дневника."""
    monkeypatch.setattr(rs, "_MAX_LIVE_DELTAS", 5)
    session = rs.RemoteSession(lambda t, ui: None)
    session.emit("user", text="напиши дълго")
    session.delta("0", start=True)
    for i in range(1, 12):
        session.delta(str(i))
    log = session.events_after(0)["events"]
    deltas = [e for e in log if e["type"] == "delta"]
    assert log[0]["type"] == "user"
    assert len(deltas) <= 5 and deltas[0].get("start") is True
    assert "".join(e["text"] for e in deltas) == "01234567891011"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(gs, "_WORKSPACE", root)
    monkeypatch.setattr(gta, "WORKSPACE", root)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    hooks.workspace = root
    yield root
    hooks.workspace = None
    gs._set_cancel(None)


def test_a_phone_turn_streams_the_model_text_then_the_final_answer(ws, monkeypatch) -> None:
    """Целият път: run_turn → ask_genesis → brain (поточно) → "delta" → "assistant"."""
    def fake_model(messages, tools=None):
        write = brain_mod._delta_writer()
        assert write is not None, "ходът от телефона не е пуснал потока"
        for part in ("Готово", " — тестовете", " минават."):
            write(part)
            time.sleep(0.07)   # по-бавно от порциите: всяка част е отделно събитие
        return "Готово — тестовете минават.", None
    monkeypatch.setattr(gta, "_ask_genesis", fake_model)
    seen: list[dict] = []
    session = rs.RemoteSession(
        lambda text, ui: gta.run_turn(deque([{"role": "system", "content": "SYS"}]), text, ui),
        on_event=seen.append)
    session.send("пусни тестовете")
    deadline = time.time() + 15
    time.sleep(0.05)
    while session.busy and time.time() < deadline:
        time.sleep(0.02)
    # Проверките след „Готово“ може да питат модела пак — гледа се първият отговор.
    answer = next(e for e in seen if e["type"] == "assistant")
    deltas = [e for e in seen if e["type"] == "delta" and e["seq"] < answer["seq"]]
    assert deltas and deltas[0].get("start") is True
    assert sum(1 for e in deltas if e.get("start")) == 1
    assert "".join(e["text"] for e in deltas) == answer["text"] == "Готово — тестовете минават."
    assert brain_mod._delta_writer() is None   # приемникът не остава след хода


def test_the_terminal_turn_is_not_streamed(ws, monkeypatch) -> None:
    """UI без `delta` (терминалът): ask_genesis не пуска поток."""
    writers: list[object] = []

    def fake_model(messages, tools=None):
        writers.append(brain_mod._delta_writer())
        return "ок", None
    monkeypatch.setattr(gta, "_ask_genesis", fake_model)

    class _UI(gta.TurnUI):
        def thinking(self, label, spinner="dots"):
            return contextlib.nullcontext()
    gta.run_turn(deque([{"role": "system", "content": "SYS"}]), "здрасти", _UI())
    assert writers == [None]
