"""`transcribe` и `speak` от телефона/Desktop (2026-10-15): гласът минава през
същия шифрован плик, говорът става текст на компютъра, а текстът не тръгва
сам — приложението го праща с `send`. Фалшиви stt/tts, без мрежа."""
from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("cryptography")

from test_remote_server import KEY, _client_open, _client_seal, _request, _session

from genesis_agent import remote_server as rs
from genesis_agent import voice


def _wav(seconds: float) -> bytes:
    n = int(seconds * 16000)
    return voice.pcm_to_wav(struct.pack(f"<{n}h", *([0] * n)))


class _Fakes:
    def __init__(self) -> None:
        self.stt_calls: list[tuple[bytes, str, str]] = []
        self.tts_calls: list[tuple[str, str | None]] = []
        self.stt_error: Exception | None = None
        self.block: threading.Event | None = None

    def stt(self, audio: bytes, mime: str, language: str) -> str:
        self.stt_calls.append((audio, mime, language))
        if self.block is not None:
            self.block.wait(5)
        if self.stt_error is not None:
            raise self.stt_error
        return "пусни тестовете"

    def tts(self, text: str, name: str | None) -> tuple[bytes, str]:
        self.tts_calls.append((text, name))
        return b"ID3-fake-mp3", "audio/mpeg"


def _server(**kw) -> tuple[rs.RemoteServer, _Fakes, rs.RemoteSession]:
    fakes = _Fakes()
    session, _ = _session()
    return rs.RemoteServer(KEY, session, name="pc", stt=fakes.stt, tts=fakes.tts, **kw), fakes, session


# ── операциите ──────────────────────────────────────────────────────────────

def test_transcribe_returns_the_text_and_does_not_send_it() -> None:
    server, fakes, session = _server()
    audio = _wav(1)
    reply = server._dispatch({"op": "transcribe", "mime": "audio/wav",
                              "data": base64.b64encode(audio).decode()})
    assert reply == {"ok": True, "text": "пусни тестовете"}
    assert fakes.stt_calls == [(audio, "audio/wav", "")]
    assert not session.busy and session.events_after(0)["events"] == []   # нищо не е изпратено
    assert "voice" in server.hello()["features"]


@pytest.mark.parametrize("encode", [
    lambda b: base64.b64encode(b).decode(),
    lambda b: base64.urlsafe_b64encode(b).decode().rstrip("="),
    lambda b: "data:audio/mp4;base64," + base64.b64encode(b).decode(),
])
def test_transcribe_accepts_the_base64_the_apps_produce(encode) -> None:
    server, fakes, _ = _server()
    raw = bytes(range(256)) * 3
    reply = server._dispatch({"op": "transcribe", "mime": "audio/m4a", "data": encode(raw),
                              "language": "bg"})
    assert reply["ok"] and fakes.stt_calls == [(raw, "audio/m4a", "bg")]


@pytest.mark.parametrize("args,error", [
    ({"mime": "audio/wav"}, "bad_args"),
    ({"mime": "audio/wav", "data": "  "}, "empty"),
    ({"mime": "audio/wav", "data": "не*е*base64"}, "bad_args"),
    ({"mime": "video/mp4", "data": "AAAA"}, "bad_mime"),
    ({"mime": 7, "data": "AAAA"}, "bad_args"),
    ({"mime": "audio/m4a", "data": "A" * (voice.MAX_AUDIO_BYTES * 4 // 3 + 100)}, "too_large"),
])
def test_transcribe_refuses_bad_requests_without_calling_stt(args, error) -> None:
    server, fakes, _ = _server()
    assert server._dispatch({"op": "transcribe", **args}) == {"ok": False, "error": error}
    assert fakes.stt_calls == []


def test_transcribe_refuses_more_than_two_minutes() -> None:
    server, fakes, _ = _server()
    data = base64.b64encode(_wav(125)).decode()
    assert server._dispatch({"op": "transcribe", "mime": "audio/wav", "data": data}) == \
        {"ok": False, "error": "too_long"}
    assert fakes.stt_calls == []


def test_a_bad_language_falls_back_to_the_default() -> None:
    server, fakes, _ = _server()
    server._dispatch({"op": "transcribe", "data": "AAAA", "language": "bg; rm -rf /"})
    assert fakes.stt_calls[0][1:] == ("audio/wav", "")


def test_a_failing_stt_is_a_reply_with_the_reason() -> None:
    server, fakes, _ = _server()
    fakes.stt_error = voice.VoiceError("Няма с какво да превърна гласа в текст")
    assert server._dispatch({"op": "transcribe", "data": "AAAA"}) == \
        {"ok": False, "error": "stt_failed", "detail": "Няма с какво да превърна гласа в текст"}


def test_speak_returns_audio_of_the_short_clean_text() -> None:
    server, fakes, _ = _server()
    reply = server._dispatch({"op": "speak", "text": "**Готово.**\n```\nкод\n```",
                              "voice": "bg-BG-BorislavNeural"})
    assert reply == {"ok": True, "mime": "audio/mpeg",
                     "data": base64.b64encode(b"ID3-fake-mp3").decode(),
                     "text": "Готово. Кодът е на екрана."}
    assert fakes.tts_calls == [("Готово. Кодът е на екрана.", "bg-BG-BorislavNeural")]
    server._dispatch({"op": "speak", "text": "Да.", "voice": "../../etc"})
    assert fakes.tts_calls[-1] == ("Да.", None)


@pytest.mark.parametrize("args,error", [
    ({}, "bad_args"), ({"text": "   "}, "empty"), ({"text": "a" * 20001}, "too_long")])
def test_speak_refuses_bad_requests(args, error) -> None:
    server, fakes, _ = _server()
    assert server._dispatch({"op": "speak", **args}) == {"ok": False, "error": error}
    assert fakes.tts_calls == []


def test_voice_ops_are_rate_limited_per_minute(monkeypatch) -> None:
    server, _fakes, _ = _server()
    now = [1000.0]
    monkeypatch.setattr(rs.time, "monotonic", lambda: now[0])
    for _ in range(rs._VOICE_PER_MIN["transcribe"]):
        assert server._dispatch({"op": "transcribe", "data": "AAAA"})["ok"]
    assert server._dispatch({"op": "transcribe", "data": "AAAA"}) == {"ok": False, "error": "rate_limited"}
    assert server._dispatch({"op": "speak", "text": "Да."})["ok"]      # отделен брояч
    now[0] += 61
    assert server._dispatch({"op": "transcribe", "data": "AAAA"})["ok"]


def test_one_transcription_at_a_time() -> None:
    server, fakes, _ = _server()
    fakes.block = threading.Event()
    first: list[dict] = []
    t = threading.Thread(target=lambda: first.append(server._dispatch({"op": "transcribe", "data": "AAAA"})))
    t.start()
    deadline = time.time() + 5
    while not fakes.stt_calls and time.time() < deadline:
        time.sleep(0.01)
    assert server._dispatch({"op": "transcribe", "data": "AAAA"}) == {"ok": False, "error": "busy"}
    fakes.block.set()
    t.join(5)
    assert first == [{"ok": True, "text": "пусни тестовете"}]


def test_without_voice_the_ops_are_unknown_and_not_advertised() -> None:
    session, _ = _session()
    server = rs.RemoteServer(KEY, session, name="pc")
    assert server._dispatch({"op": "transcribe", "data": "AAAA"}) == {"ok": False, "error": "unknown_op"}
    assert server._dispatch({"op": "speak", "text": "x"}) == {"ok": False, "error": "unknown_op"}
    assert "voice" not in server.hello()["features"]


def test_genesis_serve_turns_voice_on_with_the_real_module(monkeypatch) -> None:
    session, _ = _session()
    server = rs.RemoteServer(KEY, session, name="pc", voice=True)
    assert server.stt is rs._default_stt and server.tts is rs._default_tts
    got: list = []
    monkeypatch.setattr(voice, "transcribe", lambda a, m, lang: got.append(lang) or "т")
    assert rs._default_stt(b"x", "audio/wav", "") == "т" and got == ["bg"]   # config.yaml: bg


def test_the_phone_cannot_run_the_terminal_voice_commands() -> None:
    assert {"/voice", "/listen", "/mic"} <= rs._TERMINAL_ONLY


# ── по HTTP, с голям плик ───────────────────────────────────────────────────

@pytest.fixture()
def live_voice():
    server, fakes, _ = _server()
    httpd = server.make_http("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, server, fakes
    httpd.shutdown()
    httpd.server_close()


def _post(base: str, body: bytes) -> tuple[int, dict]:
    req = urllib.request.Request(base + "/api/v1", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _call(base: str, op: str, key: bytes = KEY, **args) -> tuple[int, dict]:
    payload = _request(op, **args)
    status, env = _post(base, json.dumps(_client_seal(key, payload)).encode())
    return status, (_client_open(key, env, payload["rid"]) if status == 200 else env)


def test_a_minute_of_audio_goes_over_http_in_one_envelope(live_voice) -> None:
    base, _, fakes = live_voice
    audio = _wav(60)                                    # 1.9 MB — над обикновения таван от 256 KiB
    data = base64.b64encode(audio).decode()
    assert len(data) > rs._MAX_BODY
    status, reply = _call(base, "transcribe", mime="audio/wav", data=data)
    assert status == 200 and reply == {"ok": True, "text": "пусни тестовете"}
    assert fakes.stt_calls[0][0] == audio
    status, reply = _call(base, "speak", text="Готово.")
    assert status == 200 and base64.b64decode(reply["data"]) == b"ID3-fake-mp3"


def test_a_big_envelope_is_only_for_voice(live_voice) -> None:
    base, server, _ = live_voice
    # 413 като преди големия плик: старите приложения показват по него
    # „твърде голямо“ (mobile/tests/interop.test.ts, 2026-10-15).
    status, reply = _call(base, "status", pad="x" * (rs._MAX_BODY + 10))
    assert status == 413 and reply == {"error": "size"}
    status, reply = _call(base, "send", text="x" * (rs._MAX_BODY + 10))
    assert status == 413 and reply == {"error": "size"}
    assert not server._failures.get("127.0.0.1")       # с ключа е — не е налучкване


def test_a_big_envelope_with_the_wrong_key_is_401_and_counted(live_voice) -> None:
    base, server, fakes = live_voice
    status, reply = _call(base, "transcribe", key=bytes(32), data="A" * (rs._MAX_BODY + 10))
    assert status == 401 and reply["error"] == "rejected" and fakes.stt_calls == []
    assert len(server._failures["127.0.0.1"]) == 1


def test_a_big_body_that_is_not_an_envelope_is_refused_unread(live_voice) -> None:
    base, server, _ = live_voice
    try:
        status, _ = _post(base, b"x" * (rs._MAX_BODY + 10))
    except (ConnectionError, urllib.error.URLError):
        status = 413                                  # RST на Windows — отказано, без четене
    assert status == 413 and len(server._failures["127.0.0.1"]) == 1


def test_a_body_over_the_voice_cap_or_without_voice_is_413(live_voice) -> None:
    base, _, _ = live_voice
    port = int(base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"POST /api/v1 HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  + f"Content-Length: {rs._MAX_VOICE_BODY + 1}\r\n\r\n".encode())
        status_line = s.recv(256).split(b"\r\n", 1)[0]
    assert b" 413 " in status_line, status_line
    session, _ = _session()
    plain = rs.RemoteServer(KEY, session, name="pc")
    httpd = plain.make_http("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # Без глас големият плик пак се чете — за `send` с картинки (2026-10-15),
        # но гласова операция в него е 413, не транскрипция.
        payload = _request("transcribe", data="A" * (rs._MAX_BODY + 10))
        status, env = _post(f"http://127.0.0.1:{httpd.server_address[1]}",
                            json.dumps(_client_seal(KEY, payload)).encode())
        assert status == 413 and env == {"error": "size"}
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_a_braked_address_cannot_upload_audio(live_voice) -> None:
    base, server, fakes = live_voice
    for _ in range(30):
        server.record_failure("127.0.0.1")
    payload = _request("transcribe", data="A" * (rs._MAX_BODY + 10))
    try:
        status, _ = _post(base, json.dumps(_client_seal(KEY, payload)).encode())
    except (ConnectionError, urllib.error.URLError):
        status = 429
    assert status == 429 and fakes.stt_calls == []
