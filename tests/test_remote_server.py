"""`genesis serve` — the phone channel. It runs commands on the operator's
machine, so what matters most here is who can talk to it: only the holder of
the key from the QR code, never a replay, never a swapped response."""
from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.request

import pytest

pytest.importorskip("cryptography")

from genesis_agent import remote_server as rs

KEY = bytes(range(32))


def _request(op: str, **args) -> dict:
    return {"ts": int(time.time() * 1000), "rid": secrets.token_hex(8), "op": op, **args}


def _client_seal(key: bytes, payload: dict) -> dict:
    return rs.Cipher(key).seal(payload, rs._REQ_AAD)


def _client_open(key: bytes, envelope: dict, rid: str) -> dict:
    return rs.Cipher(key).open(envelope, rs._RES_AAD + rid.encode())


class Recorder:
    """A runner standing in for the agent: records and emits like run_turn."""

    def __init__(self, *, confirm: bool = False, block: threading.Event | None = None) -> None:
        self.texts: list[str] = []
        self.confirm = confirm
        self.block = block
        self.session: rs.RemoteSession | None = None

    def __call__(self, text: str, ui) -> None:
        self.texts.append(text)
        if self.block is not None:
            self.block.wait(5)
        if self.confirm and self.session is not None:
            allowed = self.session.confirm("rm -rf build", ["изтрива папка"], timeout=5)
            ui.info(f"allowed={allowed}")
        if ui.cancelled():
            ui.warn("Спряно от оператора.")
            return
        ui.tool("RUN_CMD", "ok")
        ui.assistant(f"echo: {text}")


def _session(runner: Recorder | None = None) -> tuple[rs.RemoteSession, Recorder]:
    runner = runner or Recorder()
    session = rs.RemoteSession(runner)
    runner.session = session
    return session, runner


def _wait_idle(session: rs.RemoteSession) -> None:
    deadline = time.time() + 5
    while session.busy and time.time() < deadline:
        time.sleep(0.01)
    assert not session.busy


# ── the envelope ────────────────────────────────────────────────────────────

def test_round_trip_and_unicode() -> None:
    c = rs.Cipher(KEY)
    env = c.seal({"text": "здравей 👋"}, b"aad")
    assert "здравей" not in json.dumps(env)
    assert c.open(env, b"aad") == {"text": "здравей 👋"}


def test_a_different_key_cannot_open() -> None:
    env = rs.Cipher(KEY).seal({"op": "status"}, rs._REQ_AAD)
    with pytest.raises(rs.ProtocolError):
        rs.Cipher(bytes(32)).open(env, rs._REQ_AAD)


def test_a_response_cannot_be_passed_off_as_a_request() -> None:
    """Reflection: the server's own sealed reply sent back to it."""
    env = rs.Cipher(KEY).seal({"op": "send"}, rs._RES_AAD + b"abc")
    with pytest.raises(rs.ProtocolError):
        rs.Cipher(KEY).open(env, rs._REQ_AAD)


def test_a_response_is_bound_to_its_request() -> None:
    env = rs.Cipher(KEY).seal({"ok": True}, rs._RES_AAD + b"rid-one")
    with pytest.raises(rs.ProtocolError):
        rs.Cipher(KEY).open(env, rs._RES_AAD + b"rid-two")


def test_tampered_ciphertext_is_rejected() -> None:
    env = rs.Cipher(KEY).seal({"op": "status"}, rs._REQ_AAD)
    raw = bytearray(rs._b64d(env["c"]))
    raw[0] ^= 1
    env["c"] = rs._b64e(bytes(raw))
    with pytest.raises(rs.ProtocolError):
        rs.Cipher(KEY).open(env, rs._REQ_AAD)


@pytest.mark.parametrize("envelope", [None, [], {"v": 2, "n": "", "c": ""}, {"v": 1},
                                      {"v": 1, "n": "!!", "c": "!!"}])
def test_garbage_is_a_protocol_error_not_a_crash(envelope) -> None:
    with pytest.raises(rs.ProtocolError):
        rs.Cipher(KEY).open(envelope, rs._REQ_AAD)


def test_replayed_request_is_rejected() -> None:
    guard = rs.ReplayGuard()
    payload = _request("send", text="rm")
    guard.check(payload)
    with pytest.raises(rs.ProtocolError, match="replayed"):
        guard.check(payload)


def test_stale_request_is_rejected() -> None:
    guard = rs.ReplayGuard()
    payload = _request("status")
    payload["ts"] -= 10 * 60 * 1000
    with pytest.raises(rs.ProtocolError, match="stale"):
        guard.check(payload)


def test_key_is_created_once_and_private(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rs, "_config_path", lambda: tmp_path / "remote.json")
    first = rs.load_or_create_key()
    assert len(first) == 32
    assert rs.load_or_create_key() == first
    assert rs.load_or_create_key(reset=True) != first
    if hasattr(__import__("os"), "getuid"):
        assert (tmp_path / "remote.json").stat().st_mode & 0o077 == 0


def test_pairing_url_keeps_the_key_in_the_fragment() -> None:
    url = rs.pairing_url("192.168.1.5", 8765, KEY, "my pc")
    before, _, fragment = url.partition("#")
    assert rs._b64e(KEY) not in before, "the key must never reach the server in a URL"
    assert fragment.startswith("k=" + rs._b64e(KEY))
    assert "n=my%20pc" in fragment


# ── the session ────────────────────────────────────────────────────────────

def test_a_turn_becomes_events() -> None:
    session, runner = _session()
    assert session.send("hello")
    _wait_idle(session)
    kinds = [e["type"] for e in session.events_after(0)["events"]]
    assert kinds == ["user", "busy", "tool", "assistant", "busy"]
    assert runner.texts == ["hello"]


def test_one_turn_at_a_time() -> None:
    gate = threading.Event()
    session, _ = _session(Recorder(block=gate))
    assert session.send("first")
    assert not session.send("second")
    gate.set()
    _wait_idle(session)
    assert session.send("third")
    _wait_idle(session)


def test_events_long_poll_wakes_on_new_event() -> None:
    session, _ = _session()
    last = session.events_after(0)["last"]
    threading.Timer(0.2, lambda: session.emit("info", text="late")).start()
    started = time.monotonic()
    got = session.events_after(last, wait=5)
    assert got["events"][0]["text"] == "late"
    assert time.monotonic() - started < 3


def test_a_phone_that_missed_too_much_gets_the_whole_log() -> None:
    session, _ = _session()
    for i in range(rs._EVENT_LOG + 10):
        session.emit("info", text=str(i))
    got = session.events_after(3)
    assert got["reset"] is True and len(got["events"]) == rs._EVENT_LOG


def test_confirm_is_answered_from_the_phone() -> None:
    session, _ = _session(Recorder(confirm=True))
    session.send("clean")
    deadline = time.time() + 5
    while time.time() < deadline:
        pending = [e for e in session.events_after(0)["events"] if e["type"] == "confirm"]
        if pending:
            break
        time.sleep(0.01)
    assert pending[0]["operation"] == "rm -rf build"
    assert session.answer(pending[0]["id"], True)
    _wait_idle(session)
    texts = [e.get("text") for e in session.events_after(0)["events"]]
    assert "allowed=True" in texts


def test_unanswered_confirm_is_a_no() -> None:
    session, _ = _session()
    assert session.confirm("rm -rf /tmp/x", ["риск"], timeout=0.05) is False
    done = [e for e in session.events_after(0)["events"] if e["type"] == "confirm_done"]
    assert done[0]["allow"] is False


def test_stop_denies_a_waiting_confirm_and_ends_the_turn() -> None:
    session, _ = _session(Recorder(confirm=True))
    session.send("clean")
    deadline = time.time() + 5
    while not any(e["type"] == "confirm" for e in session.events_after(0)["events"]):
        assert time.time() < deadline
        time.sleep(0.01)
    session.request_stop()
    _wait_idle(session)
    texts = [e.get("text") for e in session.events_after(0)["events"]]
    assert "allowed=False" in texts and "Спряно от оператора." in texts


def test_a_crashing_turn_is_reported_and_the_session_recovers() -> None:
    def boom(_text, _ui):
        raise RuntimeError("model down")
    session = rs.RemoteSession(boom)
    session.send("x")
    _wait_idle(session)
    errors = [e for e in session.events_after(0)["events"] if e["type"] == "error"]
    assert "model down" in errors[0]["text"]
    assert session.send("again")


# ── over HTTP, the way the phone talks ─────────────────────────────────────

@pytest.fixture()
def live():
    session, _runner = _session()
    cleared: list[bool] = []
    server = rs.RemoteServer(KEY, session, name="test-pc",
                             status=lambda: {"model": "m", "workspace": "/w"},
                             clear=lambda: cleared.append(True))
    httpd = server.make_http("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, session, cleared
    httpd.shutdown()
    httpd.server_close()


def _post(base: str, body: bytes) -> tuple[int, dict]:
    req = urllib.request.Request(base + "/api/v1", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _call(base: str, op: str, key: bytes = KEY, **args) -> dict:
    payload = _request(op, **args)
    status, env = _post(base, json.dumps(_client_seal(key, payload)).encode())
    assert status == 200, env
    return _client_open(key, env, payload["rid"])


def test_hello_is_public_and_carries_no_secret(live) -> None:
    base, _, _ = live
    with urllib.request.urlopen(base + "/api/hello", timeout=5) as r:
        hello = json.loads(r.read())
    assert hello["app"] == "genesis" and hello["key_id"] == rs.key_id(KEY)
    assert rs._b64e(KEY) not in json.dumps(hello)


def test_full_conversation_over_http(live) -> None:
    base, session, cleared = live
    status = _call(base, "status")
    assert status["ok"] and status["name"] == "test-pc" and status["model"] == "m"
    assert _call(base, "send", text="здравей")["ok"]
    got = _call(base, "events", after=0, wait=5)
    seen = list(got["events"])
    while not any(e["type"] == "assistant" for e in seen):
        more = _call(base, "events", after=seen[-1]["seq"], wait=5)
        assert more["events"], "the turn never finished"
        seen += more["events"]
    assert any(e["type"] == "assistant" and e["text"] == "echo: здравей" for e in seen)
    _wait_idle(session)
    assert _call(base, "send", text="/clear")["ok"] and cleared == [True]


def test_wrong_key_is_401_and_learns_nothing(live) -> None:
    base, session, _ = live
    payload = _request("send", text="rm -rf /")
    status, body = _post(base, json.dumps(_client_seal(bytes(32), payload)).encode())
    assert status == 401 and "events" not in body
    assert session.events_after(0)["events"] == []


def test_replay_over_http_is_refused(live) -> None:
    base, session, _ = live
    payload = _request("send", text="once")
    body = json.dumps(_client_seal(KEY, payload)).encode()
    assert _post(base, body)[0] == 200
    _wait_idle(session)
    status, answer = _post(base, body)
    assert status == 401 and "replayed" in answer["detail"]
    assert [e["text"] for e in session.events_after(0)["events"] if e["type"] == "user"] == ["once"]


def test_guessing_is_slowed_down(live) -> None:
    base, _, _ = live
    junk = json.dumps({"v": 1, "n": "AAAA", "c": "AAAA"}).encode()
    codes = [_post(base, junk)[0] for _ in range(35)]
    assert codes[0] == 401 and codes[-1] == 429


def test_the_web_page_falls_back_to_a_landing_page(live) -> None:
    base, _, _ = live
    with urllib.request.urlopen(base + "/", timeout=5) as r:
        assert "Genesis" in r.read().decode("utf-8")


def test_static_files_never_leave_the_web_root(tmp_path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>app</html>")
    (tmp_path / "secret.txt").write_text("nope")
    session, _ = _session()
    server = rs.RemoteServer(KEY, session, name="x", web_root=web)
    httpd = server.make_http("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/chat"):
            with urllib.request.urlopen(base + path, timeout=5) as r:
                assert r.read() == b"<html>app</html>"
    finally:
        httpd.shutdown()
        httpd.server_close()


# ── `genesis serve` options ────────────────────────────────────────────────

def test_serve_listens_on_every_interface_by_default() -> None:
    opts = rs.parse_serve_args([])
    assert isinstance(opts, rs.ServeOptions)
    assert (opts.bind, opts.port, opts.host, opts.reset) == ("0.0.0.0", rs.DEFAULT_PORT, "", False)


def test_serve_bind_keeps_it_on_this_machine() -> None:
    # Genesis Desktop: loopback only, a port of its own, no firewall prompt.
    opts = rs.parse_serve_args(["--bind", "127.0.0.1", "--port", "0"])
    assert isinstance(opts, rs.ServeOptions)
    assert (opts.bind, opts.port) == ("127.0.0.1", 0)


@pytest.mark.parametrize("args, code", [(["--help"], 0), (["--port", "x"], 2), (["--nope"], 2)])
def test_serve_bad_or_help_options_exit(args, code, capsys) -> None:
    assert rs.parse_serve_args(args) == code
    assert "genesis serve" in capsys.readouterr().out or code == 2


# ── the terminal loop honours the phone's stop ─────────────────────────────

def test_run_turn_stops_before_the_next_tool(monkeypatch, tmp_path) -> None:
    from collections import deque

    import genesis_terminal_agent as gta

    monkeypatch.setattr(gta, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    call = {"id": "c1", "type": "function",
            "function": {"name": "RUN_CMD", "arguments": json.dumps({"command": "echo hi"})}}
    monkeypatch.setattr(gta, "ask_genesis", lambda *a, **k: ("working", [call]))
    ran: list[str] = []
    monkeypatch.setattr(gta.genesis_skills, "dispatch_tool_call",
                        lambda name, args: ran.append(name) or "ok")

    class StopUI(gta.TurnUI):
        def __init__(self) -> None:
            self.warnings: list[str] = []

        def cancelled(self) -> bool:
            return True

        def warn(self, text: str) -> None:
            self.warnings.append(text)

    ui = StopUI()
    messages = gta.run_turn(deque([{"role": "system", "content": "s"}], maxlen=50), "go", ui)
    assert ran == [], "a tool ran after stop"
    assert ui.warnings == ["Спряно от оператора."]
    assert "tool_calls" not in list(messages)[-1], "history left with unanswered tool_calls"


def test_command_op_reaches_the_commands_and_hello_says_so() -> None:
    session, _ = _session()
    calls: list[tuple] = []
    server = rs.RemoteServer(KEY, session, name="x",
                             commands=lambda n, a: calls.append((n, a)) or {"ok": True, "n": n})
    assert server.hello()["features"] == ["commands"]
    assert server._dispatch({"op": "command", "name": "usage", "arg": {"days": 7}}) == {"ok": True, "n": "usage"}
    assert server._dispatch({"op": "command", "name": "status", "arg": "junk"})["ok"]
    assert calls == [("usage", {"days": 7}), ("status", {})]


def test_command_op_is_unknown_without_commands() -> None:
    session, _ = _session()
    server = rs.RemoteServer(KEY, session, name="x")
    assert server.hello()["features"] == []
    with pytest.raises(rs.ProtocolError):
        server._dispatch({"op": "command", "name": "status"})
