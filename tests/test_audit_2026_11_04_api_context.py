"""Одит 2026-11-04 (шести кръг, 2/3): `genesis api` (llm_api) и `/context`
(context_usage), с пускане — истински сокети към ApiServer с фалшив Brain.

Всеки тест пада без поправката си (пуснато срещу стария код)."""
from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import time

import pytest

from genesis_agent import context_usage, llm_api, vision


class FakeBrain:
    def __init__(self, reply: str = "Добър ден.") -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []
        self.chain = [{"provider": "groq", "model": "fake-fast"}]
        self.current: dict | None = self.chain[0]

    def complete(self, messages, tools=None, avoid=None):
        self.calls.append(messages)
        return type("R", (), {"raw_text": self.reply, "code": "", "tool_calls": None,
                              "usage": {"prompt_tokens": 3, "completion_tokens": 2}})


@pytest.fixture
def api(monkeypatch):
    brain = FakeBrain()
    monkeypatch.setattr(llm_api, "_make_brain", lambda: brain)
    httpd = llm_api.ApiServer(0, request_timeout=5.0)
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
    port = httpd.server_address[1]

    def call(body: bytes):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, json.loads(data.decode("utf-8"))

    call.brain = brain  # type: ignore[attr-defined]
    call.port = port  # type: ignore[attr-defined]
    yield call
    httpd.shutdown()
    httpd.server_close()


# ── genesis api ─────────────────────────────────────────────────────────────

@pytest.mark.skipif(sys.platform == "win32", reason="RST може да изпревари отговора (като test_remote_voice)")
def test_a_client_still_sending_an_oversized_body_reads_the_413(api) -> None:
    """Над 64 KB сървърът отговаряше 413 и затваряше, без да изчете тялото:
    Linux праща RST и клиентът, който още праща, получава BrokenPipe вместо
    413 — измерено 0 от 10 при 1 MiB по сокет, http.client 0/5 при 24 MB."""
    size = 1024 * 1024
    for _ in range(3):
        with socket.create_connection(("127.0.0.1", api.port), timeout=5) as s:
            s.sendall(f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{api.port}\r\n"
                      f"Content-Type: application/json\r\nContent-Length: {size}\r\n\r\n".encode())
            try:
                for _ in range(size // 65536):
                    s.sendall(b"A" * 65536)
            except (BrokenPipeError, ConnectionResetError) as e:
                pytest.fail(f"клиентът не стигна до отговора: {e!r}")
            status_line = s.recv(256).split(b"\r\n", 1)[0]
        assert b" 413 " in status_line, status_line
    assert api.brain.calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="RST може да изпревари отговора (като test_remote_voice)")
def test_a_chunked_body_reads_the_411(api) -> None:
    """Тяло на парчета (генератор в http.client/requests, поток в fetch) без
    Content-Length: 411 и close() без четене — 4 MB: 5 от 5 BrokenPipe."""
    for _ in range(3):
        conn = http.client.HTTPConnection("127.0.0.1", api.port, timeout=10)
        try:
            conn.request("POST", "/v1/chat/completions", body=(b"x" * 65536 for _ in range(64)),
                         headers={"Content-Type": "application/json"}, encode_chunked=True)
            status = conn.getresponse().status
        except (BrokenPipeError, ConnectionResetError) as e:
            pytest.fail(f"клиентът не стигна до отговора: {e!r}")
        finally:
            conn.close()
        assert status == 411


def test_deeply_nested_json_is_400_not_an_internal_error(api) -> None:
    """`[` × 60 000 (под 64 KB): json.loads вдига RecursionError, който не е
    ValueError — беше 500 „Вътрешна грешка: RecursionError“ за грешка на клиента."""
    status, body = api(b"[" * 60_000)
    assert status == 400, body
    status, body = api(b'{"messages": ' + b"[" * 30_000)
    assert status == 400, body
    assert api.brain.calls == []


@pytest.mark.parametrize("content", [
    [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
    [{"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}],
    [{"type": "text", "text": "Какво виждаш?"},
     {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}],
    ["Здравей"],
])
def test_non_text_parts_are_refused_not_silently_dropped(api, content) -> None:
    """Само текстовите части се пазеха, другите изчезваха без дума: картинка
    сама стигаше до модела като празно user съобщение (`content: ""`) и
    клиентът получаваше 200 с отговор за нещо, което моделът не е видял."""
    status, body = api(json.dumps({"messages": [{"role": "user", "content": content}]}).encode())
    assert status == 400, body
    assert "текст" in body["error"]["message"]
    assert api.brain.calls == []


def test_text_parts_still_pass(api) -> None:
    content = [{"type": "text", "text": "Здра"}, {"type": "text", "text": "вей"}]
    status, _ = api(json.dumps({"messages": [{"role": "user", "content": content}]}).encode())
    assert status == 200
    assert api.brain.calls[-1] == [{"role": "user", "content": "Здравей"}]


def test_a_lone_surrogate_in_the_reply_does_not_lose_the_answer(api) -> None:
    """Отговор на модела с половин двойка (`"\\ud83d"` в JSON-а на доставчика —
    отрязано емоджи) → UnicodeEncodeError при .encode("utf-8") → 500 и
    платеният отговор се губеше."""
    api.brain.reply = "Здрасти \ud83d"
    status, body = api(json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode())
    assert status == 200, body
    assert body["choices"][0]["message"]["content"].startswith("Здрасти ")


def test_the_log_line_is_written_in_one_piece(api, monkeypatch) -> None:
    """„Един ред на заявка“: print пише реда и \\n с две отделни write — при
    40 едновременни заявки (играта пита няколко жители наведнъж) редовете се
    слепваха: „…→ 200 groq/fake 0 ms[api] …→ 200 …“ и празни редове."""
    writes: list[str] = []

    class Recorder:
        def write(self, s: str) -> int:
            writes.append(s)
            return len(s)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stdout", Recorder())
    status, _ = api(json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode())
    deadline = time.monotonic() + 5
    while not any("[api]" in w for w in writes) and time.monotonic() < deadline:
        time.sleep(0.01)
    monkeypatch.undo()
    assert status == 200
    lines = [w for w in writes if "[api]" in w]
    assert len(lines) == 1 and lines[0].endswith("\n"), writes


# ── /context ────────────────────────────────────────────────────────────────

def _png_part() -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 4000}}


def _tokens(text: str, label: str) -> float:
    line = next(ln for ln in text.splitlines() if ln.strip().startswith(label))
    value = line.split()[1].lstrip("~")
    return float(value[:-1]) * 1000 if value.endswith("K") else float(value)


def test_images_that_never_reach_the_model_are_not_counted() -> None:
    """Brain.complete маха картинките от по-стари от KEEP_IMAGE_TURNS хода
    (vision.elide_old_images) за ВСЕКИ доставчик, преди всичко друго; /context
    ги броеше всичките по ~1.5K токена. 20 хода със снимка: отчетът казваше
    ~30.0K в разговора, а към модела отиват 3 снимки (~4.5K)."""
    msgs: list[dict] = [{"role": "system", "content": "s"}]
    for i in range(20):
        msgs.append(vision.user_message(f"виж {i}", [_png_part()]))
        msgs.append({"role": "assistant", "content": "добре"})
    text = context_usage.report(msgs, [], window=128000)
    talk = _tokens(text, "Разговор")
    assert vision.KEEP_IMAGE_TURNS * vision.IMAGE_CHARS // 4 <= talk < 6000, text
    assert "твоите съобщения 20" in text


def test_tool_attachments_are_not_counted_as_operator_messages() -> None:
    """Снимката от VIEW_IMAGE / проверката в браузър е user съобщение
    „[прикачено от Genesis] …“ — броеше се към „твоите съобщения“: един ред от
    оператора и 3 снимки даваха „твоите съобщения 4 ~4.5K“."""
    msgs: list[dict] = [{"role": "system", "content": "s"},
                        {"role": "user", "content": "провери страницата"}]
    for i in range(3):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "VIEW_IMAGE", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
        msgs.append(vision.attachment_message([_png_part()], f"VIEW_IMAGE: shot{i}.png"))
    text = context_usage.report(msgs, [], window=128000)
    assert "твоите съобщения 1 ~4," in text, text
    assert "изходи на инструменти 6" in text, text
