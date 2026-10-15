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
    assert "k=" + rs._b64e(KEY) in fragment.split("&")
    assert "n=my%20pc" in fragment


def test_the_qr_code_opens_the_app_not_a_browser() -> None:
    # 2026-10-09: `http://…/#k=…` от камерата отваряше уеб версията по HTTP в LAN.
    url = rs.pairing_url("192.168.1.5", 8765, KEY, "my pc")
    assert url.startswith("genesisremote://pair#")
    assert "u=http%3A%2F%2F192.168.1.5%3A8765" in url.split("#", 1)[1].split("&")
    # Уеб версията (iPhone без приложението) — само при изрично --web.
    web = rs.pairing_url("192.168.1.5", 8765, KEY, "my pc", web=True)
    assert web.startswith("http://192.168.1.5:8765/#k=")


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


def test_a_cold_start_after_a_long_session_gets_a_bounded_reply() -> None:
    """Одит 2026-10-11: `events after=0` (всеки студен старт на приложението)
    след 1000 изхода на инструменти беше 7.3 MiB, а open() на телефона го
    декодираше 1.1–5 s на JS нишката. Сега най-новите в ~512 KiB и reset."""
    session, _ = _session()
    ui = rs.RemoteTurnUI(session)
    for i in range(1000):
        ui.tool("RUN_CMD", f"ред {i} " + "изход от теста " * 400)
    ui.assistant("последно")
    server = rs.RemoteServer(KEY, session, name="pc")
    payload = _request("events", after=0, wait=0)
    reply, rid = server.handle(_client_seal(KEY, payload))
    sealed = json.dumps(server.seal(reply, rid))
    assert len(sealed) <= 1 << 20
    opened = _client_open(KEY, json.loads(sealed), rid)
    assert opened["reset"] is True and opened["events"][-1]["text"] == "последно"
    assert opened["last"] == opened["events"][-1]["seq"]
    # Обикновеното допитване „какво има след N“ не се пипа.
    small = session.events_after(opened["last"] - 3)
    assert small["reset"] is False and len(small["events"]) == 3


def test_a_cold_start_before_the_log_rotates_gets_one_bounded_page() -> None:
    """Преглед 2026-10-11: докато дневникът не е завъртян (първото събитие е
    1), `after=0` не беше reset — над тавана телефонът теглеше дневника от
    НАЙ-СТАРИТЕ, 7 страници и 3.3 MB за 450 инструмента. Без история на
    телефона — най-новите в една страница и reset."""
    session, _ = _session()
    ui = rs.RemoteTurnUI(session)
    for i in range(450):
        ui.tool("RUN_CMD", f"ред {i} " + "изход " * 1000)
    ui.assistant("последно")
    got = session.events_after(0)
    assert got["events"][0]["seq"] > 1, "тестът иска страница под целия дневник"
    assert got["reset"] is True and got["more"] is False
    assert got["events"][-1]["text"] == "последно" and got["last"] == got["events"][-1]["seq"]
    assert len(rs.utf8_bytes(json.dumps(got["events"], ensure_ascii=False))) <= \
        rs._REPLY_BYTES + 8 * 1024
    # Празен дневник: reset без събития, телефонът няма какво да подменя.
    empty, _ = _session()
    assert empty.events_after(0)["events"] == []


def test_a_phone_that_slept_through_a_long_run_pages_from_where_it_was() -> None:
    """Одит 2026-10-11: телефонът е настигнал (after=N), заключва се, агентът
    прави 200 инструмента с кирилски изход. Над тавана сървърът пращаше
    НАЙ-НОВИТЕ с reset — дупката от ~130 събития и собствените съобщения на
    телефона изчезваха. Сега: най-старите след N, "more", без дупка."""
    session, _ = _session()
    ui = rs.RemoteTurnUI(session)
    session.emit("user", text="здравей")
    caught_up = session.events_after(0)["last"]
    session.emit("user", text="много")
    for i in range(200):
        ui.tool("RUN_CMD", f"ред {i} " + "изход " * 700)
    total = caught_up + 201
    after, pages, seqs = caught_up, 0, []
    while True:
        got = session.events_after(after)
        pages += 1
        assert got["reset"] is False
        assert len(utf8 := rs.utf8_bytes(json.dumps(got["events"], ensure_ascii=False))) <= \
            rs._REPLY_BYTES + 8 * 1024, len(utf8)
        seqs += [e["seq"] for e in got["events"]]
        assert got["last"] == got["events"][-1]["seq"]
        after = got["last"]
        if not got["more"]:
            break
    assert pages > 1
    assert seqs == list(range(caught_up + 1, total + 1)), "без дупка и без повторение"
    assert session.events_after(caught_up)["events"][0]["text"] == "много"
    # Под тавана — един отговор, "more" е false.
    assert session.events_after(total - 2)["more"] is False


def test_a_retried_send_with_the_same_mid_runs_once() -> None:
    """Одит 2026-10-11: отговорът на `send` се губи (LAN, 15 s таймаут),
    телефонът казва „няма връзка“, операторът натиска пак — ходът вървеше
    два пъти. Със същия `mid` вторият опит получава първия отговор."""
    session, runner = _session()
    server = rs.RemoteServer(KEY, session, name="pc")

    def send(text: str, mid: str | None) -> dict:
        payload = _request("send", text=text, **({"mid": mid} if mid else {}))
        reply, _ = server.handle(_client_seal(KEY, payload))
        return reply

    assert send("/plan", "m-0000000001") == {"ok": True}
    _wait_idle(session)
    assert send("/plan", "m-0000000001") == {"ok": True}
    _wait_idle(session)
    assert runner.texts == ["/plan"]
    # Друг mid (или без mid, по-старо приложение) — нов ход.
    assert send("/plan", "m-0000000002") == {"ok": True}
    _wait_idle(session)
    assert send("/plan", None) == {"ok": True}
    _wait_idle(session)
    assert runner.texts == ["/plan"] * 3


def test_a_busy_send_is_not_remembered_and_can_be_retried() -> None:
    gate = threading.Event()
    session, runner = _session(Recorder(block=gate))
    server = rs.RemoteServer(KEY, session, name="pc")

    def send(text: str, mid: str) -> dict:
        reply, _ = server.handle(_client_seal(KEY, _request("send", text=text, mid=mid)))
        return reply

    assert send("първо", "mid-first-01")["ok"]
    assert send("второ", "mid-second-1") == {"ok": False, "error": "busy"}
    gate.set()
    _wait_idle(session)
    assert send("второ", "mid-second-1") == {"ok": True}
    _wait_idle(session)
    assert runner.texts == ["първо", "второ"]


def test_remembered_mids_are_bounded() -> None:
    session, _ = _session()
    server = rs.RemoteServer(KEY, session, name="pc")
    for i in range(rs._SENT_MIDS + 5):
        payload = _request("send", text=f"т{i}", mid=f"mid-{i:08d}")
        assert server.handle(_client_seal(KEY, payload))[0] == {"ok": True}
        _wait_idle(session)
    assert len(server._sent) == rs._SENT_MIDS
    assert "mid-00000000" not in server._sent and f"mid-{rs._SENT_MIDS + 4:08d}" in server._sent


def test_events_name_the_accepted_mids() -> None:
    """Вълна 5, 2026-10-11: телефонът забравяше `mid` само по своето балонче;
    reset страница без него или „/clear“ (няма балонче) оставяха стария `mid`
    и нарочното повторение се поглъщаше. Сега `events` носи приетите `mid`."""
    gate = threading.Event()
    session, _ = _session(Recorder(block=gate))
    server = rs.RemoteServer(KEY, session, name="pc", clear=lambda: None)

    def call(op: str, **args) -> dict:
        return server.handle(_client_seal(KEY, _request(op, **args)))[0]

    assert call("send", text="първо", mid="mid-first-01")["ok"]
    assert call("send", text="второ", mid="mid-busy-001") == {"ok": False, "error": "busy"}
    gate.set()
    _wait_idle(session)
    assert call("send", text="/clear", mid="mid-clear-01")["ok"]
    mids = call("events", after=0)["mids"]
    assert mids == ["mid-first-01", "mid-clear-01"], "„busy“ не е прието"
    for i in range(rs._ACK_MIDS + 3):
        call("send", text=f"т{i}", mid=f"mid-{i:08d}")
        _wait_idle(session)
    mids = call("events", after=0)["mids"]
    assert len(mids) == rs._ACK_MIDS and mids[-1] == f"mid-{rs._ACK_MIDS + 2:08d}"


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


def test_the_first_answer_to_a_confirm_wins() -> None:
    """Две бързи докосвания („Откажи“, после „Изпълни“) не обръщат отказа."""
    import threading
    session, _ = _session()
    result: list[bool] = []
    t = threading.Thread(target=lambda: result.append(session.confirm("rm -rf build", ["риск"], timeout=5)))
    t.start()
    deadline = time.time() + 5
    while not session._pending:
        assert time.time() < deadline
        time.sleep(0.01)
    cid = next(iter(session._pending))
    assert session.answer(cid, False) is True
    assert session.answer(cid, True) is False
    t.join(5)
    assert result == [False]


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


def _post_unread(base: str, body: bytes) -> int:
    """Статусът на заявка, чието тяло сървърът нарочно не дочита (спрян адрес,
    тяло без вида на плик → 429 и затваряне, вълна 5).

    Затваряне с непрочетени байтове на Windows е RST и той може да изпревари
    429-а при клиента: на лаптопа (Python 3.14) urllib падаше с WinError 10053
    в getresponse — тестът за спряно тяло без вида на плик 3 от 3 пъти, този за
    дълбокия JSON понякога; в CI на Windows (3.12) минаваха (2026-10-12).
    И в двата случая сървърът е отказал, без да чете, затова RST се брои за 429."""
    try:
        return _post(base, body)[0]
    except ConnectionError:                       # RST при четене на отговора
        return 429
    except urllib.error.URLError as e:            # RST, докато тялото се праща
        if isinstance(e.reason, ConnectionError):
            return 429
        raise


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


def test_a_request_error_with_the_right_key_is_not_a_401(live) -> None:
    """Одит 2026-10-11: непозната операция (по-ново приложение) или текст,
    който JS trim() пази, а Python strip() изпразва ("\x1c"), бяха 401 —
    телефонът казваше „ключът вече не се приема, сдвои отново“, а 30 такива
    заключваха самия сдвоен телефон за минута."""
    base, session, _ = live
    assert _call(base, "newer_app_op") == {"ok": False, "error": "unknown_op"}
    assert _call(base, "send", text="\x1c") == {"ok": False, "error": "empty"}
    assert _call(base, "send", text=" \x85 ") == {"ok": False, "error": "empty"}
    assert _call(base, "events", after="x") == {"ok": False, "error": "bad_args"}
    for _ in range(35):
        _call(base, "newer_app_op")      # 200 всеки път, не се брои за провал
    assert _call(base, "status")["ok"]
    assert session.events_after(0)["events"] == []


def test_failures_from_the_phones_address_do_not_lock_out_the_paired_phone(live) -> None:
    """Одит 2026-10-11: спирачката беше първа и по адрес — страница в браузъра
    на самия телефон (30 POST-а text/plain, без preflight) или съсед зад
    същия NAT заключваше сдвоения телефон. Сега заявка, която се отваря с
    ключа, минава; провалите пак се броят и получават 429."""
    base, _, _ = live
    codes = [_post(base, b"x")[0] for _ in range(35)]
    assert codes[0] == 401 and codes[-1] == 429
    assert _call(base, "status")["ok"]
    assert _post(base, b"x")[0] == 429
    # Повторен (записан) плик с верния ключ не е „отворен“ — спира се.
    payload = _request("status")
    body = json.dumps(_client_seal(KEY, payload)).encode()
    assert _post(base, body)[0] == 200
    assert _post(base, body)[0] == 429


def test_a_braked_address_gets_429_before_parsing_a_body_that_is_not_an_envelope(
        live, monkeypatch, capsys) -> None:
    """Преглед 2026-10-11: след преместването на спирачката след плика всяка
    спряна заявка (240 KiB "[1,1,…]") минаваше през json.loads и handle() —
    ~10 ms работа вместо 1.5 ms. Над спирачката само тяло с вида на плик
    стига до ключа; сдвоеният телефон пак минава."""
    base, _, _ = live
    opened: list[int] = []
    real = rs.RemoteServer.handle

    def counting(self, envelope):
        opened.append(1)
        return real(self, envelope)

    monkeypatch.setattr(rs.RemoteServer, "handle", counting)
    codes = [_post(base, b"x")[0] for _ in range(31)]
    assert codes[-1] == 429
    before = len(opened)
    junk = b"[" + b"1," * (120 * 1024) + b"1]"
    assert [_post_unread(base, junk) for _ in range(5)] == [429] * 5
    assert _post(base, json.dumps({"v": 1, "n": 1, "c": []}).encode())[0] == 429
    assert len(opened) == before, "спряна заявка без вида на плик не се разчита"
    assert _call(base, "status")["ok"]
    # Срив на нишката също затваря връзката, а _post_unread брои RST за 429.
    assert "Traceback" not in capsys.readouterr().err


def test_deeply_nested_json_is_a_counted_failure_not_a_traceback(live, capsys) -> None:
    """Одит 2026-10-11: `[` × 200 000 без ключ даваше RecursionError — traceback
    в терминала на оператора на всяка заявка и нито един провал за спирачката."""
    base, _, _ = live
    # След спирачката тялото не се дочита — на Windows понякога RST (виж _post_unread).
    codes = [_post_unread(base, b"[" * 200_000) for _ in range(35)]
    assert codes[0] == 401 and codes[-1] == 429
    assert "Traceback" not in capsys.readouterr().err


def test_one_address_cannot_take_every_connection_slot(live) -> None:
    """Одит 2026-10-11: 64 празни връзки от един адрес без ключ заемаха всички
    места за 30 s — сдвоеният телефон получаваше „RemoteDisconnected“, а
    спирачката не виждаше нито един провал."""
    import http.client
    import socket
    base, _, _ = live
    port = int(base.rsplit(":", 1)[1])
    try:
        probe = socket.create_connection(("127.0.0.1", port), source_address=("127.0.0.2", 0))
        probe.close()
    except OSError:
        pytest.skip("тук няма втори loopback адрес")
    idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(64)]
    try:
        time.sleep(0.3)
        phone = http.client.HTTPConnection("127.0.0.1", port, timeout=5,
                                           source_address=("127.0.0.2", 0))
        phone.request("GET", "/api/hello")
        assert phone.getresponse().status == 200
        phone.close()
    finally:
        for sock in idle:
            sock.close()


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
    assert server.hello()["features"] == ["commands", "images"]   # картинки, 2026-10-15
    assert server._dispatch({"op": "command", "name": "usage", "arg": {"days": 7}}) == {"ok": True, "n": "usage"}
    assert server._dispatch({"op": "command", "name": "status", "arg": "junk"})["ok"]
    assert calls == [("usage", {"days": 7}), ("status", {})]


def test_command_op_is_unknown_without_commands() -> None:
    session, _ = _session()
    server = rs.RemoteServer(KEY, session, name="x")
    assert server.hello()["features"] == ["images"]
    assert server._dispatch({"op": "command", "name": "status"}) == {"ok": False, "error": "unknown_op"}


class _Timeline:
    """Къде отиде времето между Стоп и края на хода — за съобщението на теста.

    Windows CI 2026-10-15 (job 116436795903): ходът свърши 6.9 s след Стоп,
    а тестът каза само „the command ran to the end“. Само наблюдава: всяка
    обвивка вика истинската функция."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.rows: list[tuple[float, str]] = []
        self._lock = threading.Lock()

    def mark(self, what: str) -> None:
        with self._lock:
            self.rows.append((time.monotonic() - self.t0, what))

    def __str__(self) -> str:
        return "\n".join(f"{t:7.3f} s  {what}" for t, what in self.rows)

    def watch(self, monkeypatch) -> None:
        import os

        from genesis_agent import checkpoints, edit_history, sandbox
        mark = self.mark

        def timed(owner, name: str, label) -> None:
            real = getattr(owner, name)

            def wrapper(*a, **k):
                start = time.monotonic()
                try:
                    return real(*a, **k)
                finally:
                    mark(f"{label(*a)} — {time.monotonic() - start:.2f} s")
            monkeypatch.setattr(owner, name, wrapper)

        timed(checkpoints, "_git", lambda ws, *args: f"git {args[0] if args else ''}")
        timed(checkpoints, "why_not", lambda *a: "git --version (why_not)")
        timed(edit_history, "_open_window", lambda *a: "снимка за /undo преди командата")
        timed(edit_history, "after_command", lambda *a: "снимка за /undo след командата")
        timed(sandbox, "_kill_tree_win32", lambda *a: "taskkill /T /F")
        if hasattr(os, "killpg"):
            timed(sandbox.os, "killpg", lambda *a: "killpg")
        real_run = sandbox._run

        def run(argv, **k):
            mark(f"sandbox._run({argv[-1]!r})")
            res = real_run(argv, **k)
            mark(f"sandbox._run върна rc={res.returncode}: {res.stderr.strip()[-60:]!r}")
            return res
        monkeypatch.setattr(sandbox, "_run", run)

        class Job(sandbox._WinJob):
            def __init__(self, pid: int) -> None:
                super().__init__(pid)
                mark(f"командата тръгна (pid {pid}, job={'да' if self.handle else 'не'})")

            def terminate(self) -> bool:
                ok = super().terminate()
                mark(f"TerminateJobObject → {ok}")
                return ok
        monkeypatch.setattr(sandbox, "_WinJob", Job)


def _stop_a_running_sleep(monkeypatch, tmp_path, timeline: _Timeline) -> tuple[float, rs.RemoteSession]:
    """Моделът иска `sleep 8`; Стоп 1 s след „thinking“. Връща колко след Стоп
    свърши ходът."""
    from collections import deque

    import genesis_terminal_agent as gta

    monkeypatch.setattr(gta, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    gta.genesis_skills.set_workspace(tmp_path)
    call = {"id": "c1", "type": "function",
            "function": {"name": "RUN_CMD", "arguments": json.dumps({"command": "sleep 8"})}}
    replies = iter([("running tests", [call]), ("done", [])])
    monkeypatch.setattr(gta, "ask_genesis", lambda *a, **k: next(replies))
    timeline.watch(monkeypatch)
    session = rs.RemoteSession(
        lambda text, ui: gta.run_turn(deque([{"role": "system", "content": "s"}], maxlen=50), text, ui),
        on_event=lambda e: timeline.mark(
            f"събитие {e['type']} {e.get('label') or e.get('name') or e.get('busy', '')}"))
    assert session.send("run the tests")
    deadline = time.monotonic() + 10
    while not any(e["type"] == "thinking" for e in session.events_after(0)["events"]):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    time.sleep(1.0)
    stopped_at = time.monotonic()
    timeline.mark("Стоп")
    session.request_stop()
    while session.busy and time.monotonic() - stopped_at < 15:
        time.sleep(0.05)
    took = time.monotonic() - stopped_at
    timeline.mark("тестът видя края на хода")
    return took, session


def test_the_phone_stop_interrupts_a_running_command(monkeypatch, tmp_path) -> None:
    """Одит 2026-10-11: ■ на телефона не стигаше до работеща команда — ходът
    свърши 7.1 s след Стоп, `sleep 8` довърши с rc=0."""
    timeline = _Timeline()
    took, session = _stop_a_running_sleep(monkeypatch, tmp_path, timeline)
    assert took < 3, f"ходът свърши {took:.2f} s след Стоп:\n{timeline}"
    tools = [e for e in session.events_after(0)["events"] if e["type"] == "tool"]
    assert tools and "Спряно от оператора" in tools[-1]["result"], timeline


def test_a_stop_during_a_slow_undo_snapshot_ends_the_turn_in_time(monkeypatch, tmp_path) -> None:
    """Windows CI 2026-10-15 (job 116436795903): ходът свърши 6.9 s след Стоп,
    а целият тест трая 7.95 s — по-малко от самия `sleep 8`, значи командата
    не е довършила. Преди нея и след нея се снима за /undo: в свежа папка това
    са 7 + 4 последователни git процеса, нищо от тях не гледаше Стоп, а
    снимката „след“ се правеше и когато командата изобщо не беше тръгнала.
    С git по 0.7 s (11 процеса ≈ 7.9 s, колкото трая тестът на CI) същото се
    вижда на Linux: преди поправката — 6.7 s след Стоп."""
    from genesis_agent import checkpoints
    real_git, real_why_not = checkpoints._git, checkpoints.why_not

    def slow_git(*a, **k):
        time.sleep(0.7)
        return real_git(*a, **k)

    def slow_why_not(*a, **k):
        time.sleep(0.7)
        return real_why_not(*a, **k)
    monkeypatch.setattr(checkpoints, "_git", slow_git)
    monkeypatch.setattr(checkpoints, "why_not", slow_why_not)
    timeline = _Timeline()
    took, session = _stop_a_running_sleep(monkeypatch, tmp_path, timeline)
    assert took < 3, f"ходът свърши {took:.2f} s след Стоп:\n{timeline}"
    tools = [e for e in session.events_after(0)["events"] if e["type"] == "tool"]
    # Стоп дойде, докато се снимаше — командата не е пусната изобщо.
    assert tools and "не е пусната" in tools[-1]["result"], timeline


def test_after_a_stop_during_a_tool_the_model_is_not_asked_again(monkeypatch, tmp_path) -> None:
    """2026-10-15: след Стоп по време на инструмент моделът се питаше още веднъж
    („Анализирам…“) и чак отговорът му спираше хода — с истински модел това са
    секунди на „Genesis работи…“ и платено обръщение след Стоп."""
    from collections import deque

    import genesis_terminal_agent as gta

    monkeypatch.setattr(gta, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    gta.genesis_skills.set_workspace(tmp_path)
    call = {"id": "c1", "type": "function",
            "function": {"name": "RUN_CMD", "arguments": json.dumps({"command": "make"})}}
    asked: list[int] = []
    replies = iter([("running", [call]), ("done", [])])

    def ask(*a, **k):
        asked.append(1)
        return next(replies)
    monkeypatch.setattr(gta, "ask_genesis", ask)
    holder: dict = {}
    session = rs.RemoteSession(lambda text, ui: holder.__setitem__("msgs", gta.run_turn(
        deque([{"role": "system", "content": "s"}], maxlen=50), text, ui)))

    def dispatch(name, args):
        session.request_stop()          # Стоп идва, докато командата върви
        return "[RUN_CMD: make]  (rc=None)\n[sandbox] ⏹ Спряно от оператора"
    monkeypatch.setattr(gta.genesis_skills, "dispatch_tool_call", dispatch)
    assert session.send("build")
    deadline = time.monotonic() + 10
    while session.busy and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(asked) == 1
    tool_msgs = [m for m in holder["msgs"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and "Спряно" in tool_msgs[0]["content"]


def test_the_window_sees_which_tool_is_running(monkeypatch, tmp_path) -> None:
    """Проба на приложението 2026-10-12: докато `sleep 60` вървеше, прозорецът
    показваше само „Genesis мисли...“ — събитие идваше чак след инструмента."""
    from collections import deque

    import genesis_terminal_agent as gta

    monkeypatch.setattr(gta, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(gta, "_remember", lambda *a: None)
    gta.genesis_skills.set_workspace(tmp_path)
    call = {"id": "c1", "type": "function",
            "function": {"name": "RUN_CMD", "arguments": json.dumps({"command": "echo hi"})}}
    replies = iter([("", [call]), ("готово", [])])
    monkeypatch.setattr(gta, "ask_genesis", lambda *a, **k: next(replies))
    session = rs.RemoteSession(lambda text, ui: gta.run_turn(
        deque([{"role": "system", "content": "s"}], maxlen=50), text, ui))
    assert session.send("пусни го")
    deadline = time.monotonic() + 20
    while session.busy and time.monotonic() < deadline:
        time.sleep(0.05)
    events = session.events_after(0)["events"]
    kinds = [(e["type"], e.get("label") or e.get("name")) for e in events]
    assert ("thinking", "RUN_CMD: echo hi") in kinds, kinds
    assert kinds.index(("thinking", "RUN_CMD: echo hi")) < kinds.index(("tool", "RUN_CMD"))


def test_a_replay_is_still_rejected_after_a_restart(tmp_path) -> None:
    """Одит 2026-10-07: видяното беше само в паметта — след рестарт на
    `genesis serve` записано съобщение се изпълняваше пак."""
    path = tmp_path / "remote_seen.json"
    payload = _request("send", text="rm -rf build && deploy")
    rs.ReplayGuard(path=path).check(payload)
    with pytest.raises(rs.ProtocolError, match="replayed"):
        rs.ReplayGuard(path=path).check(payload)          # нов процес, същият файл


def test_a_broken_seen_file_does_not_stop_the_server(tmp_path) -> None:
    path = tmp_path / "remote_seen.json"
    path.write_text("{not json", encoding="utf-8")
    rs.ReplayGuard(path=path).check(_request("status"))


# ── одит на QR сдвояването (2026-10-09) ──────────────────────────────────────

def test_an_ipv6_host_gets_brackets_in_the_code() -> None:
    url = rs.pairing_url("fd7a:115c:a1e0::1", 8765, KEY, "pc")
    assert "u=http%3A%2F%2F%5Bfd7a%3A115c%3Aa1e0%3A%3A1%5D%3A8765" in url


@pytest.fixture
def no_listen(monkeypatch):
    """serve() никога не слуша и не пипа глобалния sandbox — ако проверката
    липсва, тестът пада (код 1), вместо да виси на serve_forever."""
    from genesis_agent import sandbox

    def refuse(self, host, port):
        raise OSError("тест: без слушане")
    monkeypatch.setattr(rs.RemoteServer, "make_http", refuse)
    monkeypatch.setattr(sandbox, "set_policy", lambda policy: None)


@pytest.mark.parametrize("host", ["PC_1.local", "user@pc", "pc/x", "pc local"])
def test_serve_refuses_a_host_the_phone_cannot_read(host, capsys, no_listen) -> None:
    assert rs.serve(["--host", host]) == 2
    assert "--host" in capsys.readouterr().out


def test_serve_web_without_the_web_build_says_so(monkeypatch, capsys, no_listen) -> None:
    monkeypatch.setattr(rs, "_web_root", lambda: None)
    assert rs.serve(["--web"]) == 2
    assert "--web" in capsys.readouterr().out


# ── a file name that is not UTF-8 ───────────────────────────────────────────

def test_a_non_utf8_file_name_does_not_break_the_event_stream(live) -> None:
    """Одит 2026-10-11: име на файл в cp1251 („отчет.txt“) стига до LIST_DIR като
    lone surrogates; seal() гърмеше с UnicodeEncodeError и всяко `events after=0`
    затваряше връзката — телефонът показваше „няма връзка“ до края на сесията."""
    base, session, _ = live
    rs.RemoteTurnUI(session).tool("LIST_DIR", "  main.py\n  \udcee\udcf2\udcf7\udce5\udcf2.txt")
    rs.RemoteTurnUI(session).assistant("Готово.")
    for _ in range(2):
        got = _call(base, "events", after=0, wait=0)
        tools = [e for e in got["events"] if e["type"] == "tool"]
        assert tools and "main.py" in tools[0]["result"] and ".txt" in tools[0]["result"]
        assert got["events"][-1]["text"] == "Готово."


# ── Вълна 5 (2026-10-11): повторна проверка на вълна 4 ─────────────────────

def test_a_single_huge_event_does_not_break_the_reply_cap() -> None:
    """Едно събитие над тавана влизаше цяло: отговор от 600 000 знака даваше
    1.2 MB при студен старт. Сега текстът се орязва и се белязва."""
    for after in (0, 1):
        session, _ = _session()
        session.emit("user", text="кажи много")
        session.emit("assistant", text="я" * 600_000)
        got = session.events_after(after)
        size = len(rs.utf8_bytes(json.dumps(got, ensure_ascii=False)))
        assert size <= rs._REPLY_BYTES + 8 * 1024, (after, size)
        last = got["events"][-1]
        assert last["type"] == "assistant" and last["clipped"] is True
        assert last["text"].startswith("яяя") and "терминала" in last["text"]
        # Дневникът не се пипа: следващият отговор пак реже копие.
        assert len(session._events[-1]["text"]) == 600_000
    # Всяко поле от управляващи знаци (6 байта в JSON) също влиза.
    session, _ = _session()
    session.emit("assistant", text="\x01" * 300_000)
    size = len(rs.utf8_bytes(json.dumps(session.events_after(0), ensure_ascii=False)))
    assert size <= rs._REPLY_BYTES + 8 * 1024, size


def _braked(base: str) -> None:
    codes = [_post(base, b"x")[0] for _ in range(31)]
    assert codes[-1] == 429


def _raw_post(base: str, declared: int, sent: bytes, wait: float) -> tuple[bytes, float]:
    """POST с обявена дължина `declared`, от която са пратени само `sent`."""
    import socket
    port = int(base.rsplit(":", 1)[1])
    sock = socket.create_connection(("127.0.0.1", port), timeout=wait)
    try:
        sock.sendall(b"POST /api/v1 HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                     + f"Content-Length: {declared}\r\n\r\n".encode() + sent)
        started = time.monotonic()
        try:
            got = sock.recv(4096)
        except (TimeoutError, ConnectionError):
            got = b""
        return got, time.monotonic() - started
    finally:
        sock.close()


def test_a_braked_address_is_refused_before_its_body_is_read(live) -> None:
    """Преглед на вълна 4: над спирачката сървърът пак четеше цялото тяло
    (до 256 KiB) — бавен клиент, който обяви 200 KiB и прати 1 KiB, държеше
    нишка и място 30 s за всяка заявка. Преди вълна 4 спряният адрес получаваше
    429 без четене. Сега стига началото на тялото: не е плик → 429 веднага."""
    base, _, _ = live
    _braked(base)
    got, took = _raw_post(base, 200 * 1024, b"[" + b"1," * 512, wait=5)
    assert got.startswith((b"HTTP/1.0 429", b"HTTP/1.1 429")), got[:40]
    assert took < 2.0, took
    # Сдвоеният телефон зад същия адрес пак минава.
    assert _call(base, "status")["ok"]


def test_a_braked_address_that_sends_no_body_is_dropped_fast(live) -> None:
    base, _, _ = live
    _braked(base)
    _, took = _raw_post(base, 200 * 1024, b"", wait=10)
    assert took < 5.0, took
    assert _call(base, "status")["ok"]


# Начало на плик: публично, ~30 байта, минава _ENVELOPE_HEAD.
_ENVELOPE_PREFIX = b'{"v":1,"n":"abcdefgh","c":"' + b"A" * 200


def _open_post(base: str, declared: int, sent: bytes):
    import socket
    port = int(base.rsplit(":", 1)[1])
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(b"POST /api/v1 HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                 + f"Content-Length: {declared}\r\n\r\n".encode() + sent)
    return sock


def _closed_within(sock, seconds: float, trickle: bytes = b"") -> float | None:
    """След колко секунди сървърът отговаря или затваря (None — не го прави).
    С `trickle` праща по един байт на 1.5 s, докато чака."""
    started = time.monotonic()
    try:
        while time.monotonic() - started < seconds:
            sock.settimeout(1.5)
            try:
                sock.recv(4096)
                return time.monotonic() - started
            except TimeoutError:
                if trickle:
                    sock.sendall(trickle)
            except ConnectionError:
                return time.monotonic() - started
        return None
    finally:
        sock.close()


def test_a_braked_envelope_prefix_that_stalls_is_dropped(live) -> None:
    """Ревю на вълна 5 (2026-10-11): спрян адрес, пратил публичното начало на
    плик и спрял, се четеше нататък с 30 s на всяко recv — държеше нишка и
    място на адреса; 8 s по-късно връзката още беше отворена."""
    base, _, _ = live
    _braked(base)
    took = _closed_within(_open_post(base, 200 * 1024, _ENVELOPE_PREFIX), 8)
    assert took is not None and took < rs._BRAKED_READ_S + 1.5, took


def test_a_braked_trickle_is_dropped_on_a_total_deadline(live) -> None:
    """Таймаутът беше за едно recv: байт на 1.5 s не стигаше до 429 и след 12 s."""
    base, _, _ = live
    _braked(base)
    took = _closed_within(_open_post(base, 200 * 1024, b"["), 9, trickle=b"1")
    assert took is not None and took < rs._BRAKED_READ_S + 2, took


def test_stalled_braked_envelopes_do_not_lock_out_the_phone(live) -> None:
    """16 спрели начала на плик заемаха всички места на адреса: сдвоеният
    телефон 3 s по-късно получаваше затворена връзка без отговор."""
    base, _, _ = live
    _braked(base)
    socks = [_open_post(base, 200 * 1024, _ENVELOPE_PREFIX)
             for _ in range(rs._QuietServer._MAX_PER_IP)]
    try:
        time.sleep(rs._BRAKED_READ_S + 1.5)
        assert _call(base, "status")["ok"]
    finally:
        for sock in socks:
            sock.close()
