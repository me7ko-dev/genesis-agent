"""`send` с "images" от телефона и Desktop (2026-10-15): проверка, тавани,
отделният адрес за голям плик и пътят до хода. Без мрежа извън 127.0.0.1."""
from __future__ import annotations

import base64
import io
import json
import threading
import urllib.error
import urllib.request
from collections import deque

import pytest

pytest.importorskip("cryptography")

from genesis_agent import chat_commands, sandbox, vision
from genesis_agent import remote_server as rs
from tests.test_remote_server import KEY, _client_open, _client_seal, _request
from tests.test_vision import png


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "_POLICY", sandbox._POLICY)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    (tmp_path / "shot.png").write_bytes(png())
    return tmp_path




class ImageRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list | None]] = []

    def __call__(self, text, ui, images=None) -> None:
        self.calls.append((text, images))
        ui.assistant("ok")


def _server() -> tuple:
    runner = ImageRecorder()
    session = rs.RemoteSession(runner)
    return rs.RemoteServer(KEY, session, name="pc"), session, runner


def _wait(session) -> None:
    import time
    deadline = time.time() + 5
    while session.busy and time.time() < deadline:
        time.sleep(0.01)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_send_with_images_reaches_the_turn(monkeypatch) -> None:
    server, session, runner = _server()
    reply = server._dispatch({"op": "send", "text": "какво е това?", "mid": "m-123456789",
                              "images": [{"mime": "image/png", "data": _b64(png()), "name": "a.png"}]})
    assert reply == {"ok": True}
    _wait(session)
    text, images = runner.calls[0]
    assert text == "какво е това?" and images and images[0]["genesis"]["name"] == "a.png"
    user = next(e for e in session.events_after(0)["events"] if e["type"] == "user")
    assert user["images"] == [{"name": "a.png", "mime": "image/png", "bytes": len(png()),
                               "width": 4, "height": 3}]
    assert "data" not in json.dumps(user["images"])


def test_send_with_only_images_asks_the_default_question() -> None:
    server, session, runner = _server()
    assert server._dispatch({"op": "send", "text": "",
                             "images": [{"data": _b64(png())}]}) == {"ok": True}
    _wait(session)
    assert runner.calls[0][0] == chat_commands.IMAGE_QUESTION


def test_send_without_images_keeps_the_old_runner_call() -> None:
    server, session, runner = _server()
    assert server._dispatch({"op": "send", "text": "здрасти"}) == {"ok": True}
    _wait(session)
    assert runner.calls == [("здрасти", None)]


def test_send_images_limits() -> None:
    server, _session, runner = _server()
    five = [{"data": _b64(png())}] * 5
    assert server._dispatch({"op": "send", "text": "x", "images": five})["error"] == "too_many_images"
    # Общо до 10 MiB: колкото аудиото в същия голям плик (19 MiB, voice).
    half = "A" * ((rs._MAX_IMAGES_BYTES // 2 + 1024) * 4 // 3)
    reply = server._dispatch({"op": "send", "text": "x",
                              "images": [{"data": half, "name": "a.png"}, {"data": half}]})
    assert reply == {"ok": False, "error": "images_too_large", "max": rs._MAX_IMAGES_BYTES}
    reply = server._dispatch({"op": "send", "text": "x",
                              "images": [{"data": _b64(b"%PDF-1.4"), "name": "c.png"}]})
    assert reply["error"] == "bad_image" and "не е PNG" in reply["detail"]
    reply = server._dispatch({"op": "send", "text": "x", "images": [{"data": "@@@", "name": "d"}]})
    assert reply["error"] == "bad_image" and reply["detail"] == "невалиден base64"
    assert server._dispatch({"op": "send", "text": "x", "images": "nope"})["error"] == "bad_args"
    assert runner.calls == []


def test_four_images_fit_in_the_large_envelope() -> None:
    worst = rs._MAX_IMAGES_BYTES * 16 // 9 + 20000 * 6 + 4096   # base64², текст, полета
    assert worst < rs._MAX_VOICE_BODY


def test_url_safe_base64_like_the_audio() -> None:
    server, session, runner = _server()
    data = base64.urlsafe_b64encode(png()).decode().rstrip("=")
    assert server._dispatch({"op": "send", "text": "x", "images": [{"data": data}]}) == {"ok": True}
    _wait(session)
    assert runner.calls[0][1]


def test_hello_advertises_images_and_limits() -> None:
    server, _s, _r = _server()
    hello = server.hello()
    assert "images" in hello["features"]
    assert hello["limits"] == {"images": 4, "images_bytes": 10 * 1024 * 1024,
                               "body": rs._MAX_BODY, "large_body": rs._MAX_VOICE_BODY}


@pytest.fixture()
def live_images():
    server, session, runner = _server()
    httpd = server.make_http("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", session, runner
    httpd.shutdown()
    httpd.server_close()


def _post(url: str, body: bytes) -> tuple[int, dict]:
    req = urllib.request.Request(url + "/api/v1", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _photo(size: int) -> bytes:
    """PNG, раздут до `size` байта с ancillary chunk (декодира се нормално)."""
    import struct
    import zlib
    data = png(64, 64)
    pad = b"\x00" * max(0, size - len(data) - 12)
    chunk = struct.pack(">I", len(pad)) + b"tEXt" + pad + struct.pack(
        ">I", zlib.crc32(b"tEXt" + pad) & 0xFFFFFFFF)
    return data[:33] + chunk + data[33:]


def test_a_large_envelope_with_images_goes_through_without_voice(live_images, monkeypatch) -> None:
    # Сървър без глас (stt=None): големият плик е и за картинките (2026-10-15).
    monkeypatch.setattr(vision, "MAX_SEND_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(vision, "_pillow", lambda: None)
    base, session, runner = live_images
    payload = _request("send", text="виж", images=[{"mime": "image/png", "data": _b64(_photo(400_000)),
                                                    "name": "p.png"}])
    body = json.dumps(_client_seal(KEY, payload)).encode()
    assert len(body) > rs._MAX_BODY
    status, env = _post(base, body)
    assert status == 200
    assert _client_open(KEY, env, payload["rid"]) == {"ok": True}
    _wait(session)
    assert runner.calls[0][1][0]["genesis"]["name"] == "p.png"


def test_a_large_envelope_is_still_refused_for_text_and_other_ops(live_images) -> None:
    base, _session, runner = live_images
    for payload in (_request("status", pad="x" * 300_000),
                    _request("send", text="x" * 300_000),
                    _request("send", text="x", images=[], pad="x" * 300_000)):
        status, env = _post(base, json.dumps(_client_seal(KEY, payload)).encode())
        assert status == 200
        assert _client_open(KEY, env, payload["rid"]) == {"ok": False, "error": "too_large"}
    assert runner.calls == []


def test_the_phone_command_path_collects_image_command_and_at_images(ws) -> None:
    session = rs.RemoteSession(ImageRecorder())
    images: list[dict] = []
    ui = rs.RemoteTurnUI(session)
    _m, prompt = rs.phone_command("/image shot.png какво е", ui, session, messages=deque(),
                                        workspace=ws, images=images)
    assert prompt == "какво е" and len(images) == 1
    images = []
    _m, prompt = rs.phone_command("виж @shot.png", ui, session, messages=deque(), workspace=ws,
                                  images=images)
    assert prompt == "виж @shot.png" and len(images) == 1
