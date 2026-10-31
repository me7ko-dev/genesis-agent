"""Одит 2026-10-31 (шести кръг): genesis_agent/mcp_oauth.py — входът с OAuth
за MCP сървъри: адрес със заявка (`?tenant=`), сървър за вход с път (Okta),
общ срок и таван на HTTP размяната с него, грешка на flock, която не е
„зает“, клиент с тайна от регистрацията, грешките на /token, адресът за вход
в терминала и /mcp logout на вход само в паметта. Всеки тест падаше срещу
кода преди поправката (възпроизведено със скриптовете на одита, audit6/),
освен отбелязаните „пазач“ — те държат отказа, който поправката запазва."""
from __future__ import annotations

import errno
import io
import json
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import genesis_skills as gs
from genesis_agent import chat_commands, mcp_client, mcp_oauth, paths


def _browser(url: str) -> None:
    """Операторът в браузъра: влиза и сървърът го праща обратно към Genesis."""
    import requests
    requests.get(url, timeout=10, allow_redirects=True)


def _login(url: str, **kw) -> dict:
    return mcp_oauth.login(url, open_browser=kw.pop("open_browser", _browser),
                           say=lambda _t: None, timeout=20, **kw)


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    import fake_oauth_mcp
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(gs, "_WORKSPACE", tmp_path)
    started = []

    def start(**kw):
        url, httpd, state = fake_oauth_mcp.serve(**kw)
        started.append(httpd)
        (Path(paths.GENESIS_HOME) / "mcp.json").write_text(
            json.dumps({"mcpServers": {"linear": {"type": "http", "url": url}}}), encoding="utf-8")
        return url, state
    yield start
    mcp_client.stop_all()
    for h in started:
        h.shutdown()


@pytest.fixture
def local(monkeypatch):
    """Само 127.0.0.1, без прокси."""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _tiny_server(get=None, post=None, delete=None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, obj, ctype="application/json"):
            body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            get(self)

        def do_POST(self):
            self.raw_body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            post(self)

        def do_DELETE(self):
            delete(self)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class _RawServer:
    """Сървър на голи сокети: `stall` — приема и мълчи; `trickle` — праща
    отговора по байт (зад прокси, който е заседнал, или бъгав сървър)."""

    def __init__(self, mode: str, every: float = 0.3, headers_too: bool = False) -> None:
        self.mode, self.every, self.headers_too = mode, every, headers_too
        self.stop = threading.Event()
        self.conns: list[socket.socket] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/token"
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.recv(65536)
            if self.mode == "stall":
                self.stop.wait()
                return
            head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n"
            if not self.headers_too:
                conn.sendall(head)
                head = b""
            for byte in head + b" " * 100000:
                if self.stop.wait(self.every):
                    return
                conn.sendall(bytes([byte]))
        except OSError:
            pass

    def close(self) -> None:
        self.stop.set()
        for c in [*self.conns, self.sock]:
            try:
                c.close()
            except OSError:
                pass


def _expired_entry(server_url: str, token_endpoint: str, token: str = "cached") -> None:
    mcp_oauth._save_all({mcp_oauth._key(server_url): {
        "access_token": token, "refresh_token": "r1", "expires_at": time.time() - 10,
        "lifetime": 3600.0, "client_id": "c1", "token_endpoint": token_endpoint,
        "resource": mcp_oauth._key(server_url), "issuer": token_endpoint.rsplit("/", 1)[0]}})


# ── a1: адрес със заявка при сървър без метаданни за ресурса ──────────────────

def test_a_legacy_server_url_with_a_query_can_log_in(oauth) -> None:
    # Сървър от 2025-03-26 (без /.well-known/oauth-protected-resource) на
    # `…/mcp?tenant=acme`: подразбиращият се `resource` носеше заявката и
    # входът винаги падаше с „иска токен за X, а е X — отказано“.
    url, state = oauth(legacy=True)
    with_query = url + "?tenant=acme"
    assert mcp_oauth.discover(with_query)["resource"] == url
    entry = _login(with_query)
    assert entry["access_token"] in state["tokens"]
    assert state["resource_seen"] == [url]
    assert mcp_oauth.access_token(with_query) == entry["access_token"]


def test_a_server_supplied_resource_with_a_query_is_still_refused(local) -> None:
    # Пазач: отказът на заявка в `resource` остава за този, който сървърът дава.
    holder: dict = {}

    def get(h):
        b = holder["base"]
        if h.path.startswith("/.well-known/oauth-protected-resource"):
            return h._send(200, {"resource": b + "/mcp?tenant=other", "authorization_servers": [b]})
        if h.path == "/.well-known/oauth-authorization-server":
            return h._send(200, {"issuer": b, "authorization_endpoint": b + "/a",
                                 "token_endpoint": b + "/t"})
        return h._send(404, {})
    httpd, base = _tiny_server(get)
    holder["base"] = base
    try:
        with pytest.raises(mcp_oauth.OAuthError, match="отказано"):
            mcp_oauth.discover(base + "/mcp?tenant=acme")
    finally:
        httpd.shutdown()


# ── a3: сървър за вход с път, чийто хост има метаданни за друг издател ───────

def test_an_issuer_with_a_path_skips_the_hosts_other_issuer(local) -> None:
    # Okta: `…/oauth2/default` е свой сървър за вход, а коренът на хоста
    # описва този на организацията (issuer = хоста). Откриването спираше на
    # корена и отказваше „метаданните са за …“; верният адрес не се питаше.
    holder: dict = {}

    def get(h):
        b = holder["base"]
        if h.path == "/.well-known/oauth-protected-resource/mcp":
            return h._send(200, {"resource": b + "/mcp",
                                 "authorization_servers": [b + "/oauth2/default"]})
        if h.path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
            return h._send(200, {"issuer": b, "authorization_endpoint": b + "/oauth2/v1/authorize",
                                 "token_endpoint": b + "/oauth2/v1/token"})
        if h.path == "/oauth2/default/.well-known/openid-configuration":
            c = b + "/oauth2/default"
            return h._send(200, {"issuer": c, "authorization_endpoint": c + "/v1/authorize",
                                 "token_endpoint": c + "/v1/token"})
        return h._send(404, {})
    httpd, base = _tiny_server(get)
    holder["base"] = base
    try:
        info = mcp_oauth.discover(base + "/mcp")
        assert info["token_endpoint"] == base + "/oauth2/default/v1/token"
        assert info["issuer"] == base + "/oauth2/default"
    finally:
        httpd.shutdown()


# ── a4: общ срок и таван на HTTP размяната със сървъра за вход ───────────────

@pytest.mark.parametrize("headers_too", [False, True], ids=["body", "headers"])
def test_a_trickling_token_endpoint_does_not_hold_the_lock(local, monkeypatch, headers_too) -> None:
    # Сървър за токени, който капе по байт: срокът на requests е за едно recv,
    # refresh() висеше (с _lock) завинаги — /mcp logout и изходът замръзваха.
    monkeypatch.setattr(mcp_oauth, "_HTTP_TIMEOUT", 1.5)
    raw = _RawServer("trickle", headers_too=headers_too)
    server_url = "http://127.0.0.1:9/mcp"
    _expired_entry(server_url, raw.url)
    got: list[str] = []
    worker = threading.Thread(target=lambda: got.append(mcp_oauth.access_token(server_url)),
                              daemon=True)
    start = time.monotonic()
    try:
        worker.start()
        worker.join(8)
        assert not worker.is_alive(), "access_token() още чака капещия сървър"
        assert got == [""] and time.monotonic() - start < 6
        assert not mcp_oauth._lock.locked()
    finally:
        raw.close()
        worker.join(20)


def test_a_huge_token_response_is_refused(local) -> None:
    # 100 MB тяло влизаше цялото в паметта и в mcp_tokens.json.
    huge = json.dumps({"access_token": "A" * 2_000_000, "token_type": "Bearer"}).encode()
    httpd, base = _tiny_server(post=lambda h: h._send(200, huge))
    server_url = "http://127.0.0.1:9/mcp"
    try:
        _expired_entry(server_url, base + "/token")
        assert mcp_oauth.refresh(server_url) is None
        assert mcp_oauth.saved(server_url)["access_token"] == "cached"
    finally:
        httpd.shutdown()


def _mcp_answering(seen: list[str]):
    def post(h):
        seen.append(h.headers.get("Authorization", ""))
        msg = json.loads(h.raw_body)
        h._send(200, {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"tools": []}})

    def delete(h):
        seen.append("DELETE " + h.headers.get("Authorization", ""))
        h._send(200, {})
    return _tiny_server(post=post, delete=delete)


def test_closing_a_session_does_not_wait_for_a_stalled_token_endpoint(local) -> None:
    # stop() (и stop_all() при atexit) подновяваше изтекъл токен преди DELETE:
    # 15 s на сървър, 45 s за три — при срок на DELETE 3 s.
    raw = _RawServer("stall")
    seen: list[str] = []
    httpd, base = _mcp_answering(seen)
    try:
        _expired_entry(base + "/mcp", raw.url)
        srv = mcp_client.HttpServer("linear", "", [], {}, set(), Path("x"), url=base + "/mcp")
        srv._ready, srv.session_id = True, "s1"
        start = time.monotonic()
        srv.stop()
        assert time.monotonic() - start < 3
        assert seen == ["DELETE Bearer cached"]           # затваря с каквото има
    finally:
        raw.close()
        httpd.shutdown()


def test_a_call_does_not_wait_for_a_refresh_past_its_deadline(local) -> None:
    # Подновяването в _headers() е преди POST-а и не гледаше срока на
    # извикването: извикване със срок 2 s чакаше 15 s сървъра за вход.
    raw = _RawServer("stall")
    seen: list[str] = []
    httpd, base = _mcp_answering(seen)
    try:
        _expired_entry(base + "/mcp", raw.url)
        srv = mcp_client.HttpServer("linear", "", [], {}, set(), Path("x"), url=base + "/mcp")
        start = time.monotonic()
        try:
            srv.request("tools/list", deadline=time.monotonic() + 2)
        except mcp_client.MCPError as e:
            assert "навреме" in str(e)       # срокът отиде за подновяването — но е спазен
        assert time.monotonic() - start < 4
    finally:
        raw.close()
        httpd.shutdown()


# ── a5: грешка на flock, която не е „зает“ ───────────────────────────────────

def test_a_lock_error_that_is_not_busy_does_not_wait(oauth, monkeypatch) -> None:
    # ENOLCK (NFS без lockd): 60 s чакане, „зает от друг процес“ и изхвърлен
    # токен, който сървърът вече е издал.
    url, state = oauth()

    def no_locks(_fh):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(mcp_oauth, "_lock_file", no_locks)
    start = time.monotonic()
    entry = _login(url)
    assert time.monotonic() - start < 10
    assert entry["access_token"] in state["tokens"]
    assert mcp_oauth.saved(url)["access_token"] == entry["access_token"]
    assert not mcp_oauth._lock.locked()


def test_a_busy_lock_after_login_keeps_the_token_in_this_process(oauth, monkeypatch) -> None:
    url, state = oauth()

    def busy(_fh):
        raise OSError(errno.EWOULDBLOCK, "Resource temporarily unavailable")
    monkeypatch.setattr(mcp_oauth, "_lock_file", busy)
    monkeypatch.setattr(mcp_oauth, "_LOCK_WAIT", 0.5, raising=False)
    entry = _login(url)
    assert entry["access_token"] in state["tokens"]
    assert mcp_oauth.access_token(url) == entry["access_token"]


# ── a6: тайна на клиента от динамичната регистрация ─────────────────────────

@pytest.mark.parametrize("method", ["client_secret_post", "client_secret_basic"])
def test_a_client_secret_from_registration_is_used(oauth, method) -> None:
    # RFC 7591 §3.2.1: сървърът може да смени поискания `none`. Тайната се
    # губеше и размяната на кода падаше с 401 СЛЕД входа в браузъра.
    url, state = oauth(confidential=method)
    entry = _login(url)
    assert entry["access_token"] in state["tokens"]
    new = mcp_oauth.refresh(url, stale=entry["access_token"])
    assert new and new != entry["access_token"] and new in state["tokens"]
    assert state["client_auth"] == [method, method]


def test_an_unsupported_client_auth_is_refused_before_the_browser(oauth) -> None:
    url, _ = oauth(confidential="private_key_jwt")
    opened: list[str] = []
    with pytest.raises(mcp_oauth.OAuthError, match="private_key_jwt"):
        _login(url, open_browser=opened.append)
    assert opened == []


# ── a9: грешките на /token ───────────────────────────────────────────────────

def test_a_non_json_token_error_says_the_http_status(local) -> None:
    page = b"<html><body><h1>502 Bad Gateway</h1></body></html>"
    httpd, base = _tiny_server(post=lambda h: h._send(502, page, "text/html"))
    try:
        with pytest.raises(mcp_oauth.OAuthError) as e:
            mcp_oauth._token_request(base + "/token", {"grant_type": "refresh_token"})
        assert "HTTP 502" in str(e.value) and "Expecting value" not in str(e.value)
    finally:
        httpd.shutdown()


def test_a_long_error_description_is_cut(local) -> None:
    desc = "грешка\n\t" + "A" * 50_000
    httpd, base = _tiny_server(post=lambda h: h._send(400, {"error": "invalid_grant",
                                                            "error_description": desc}))
    try:
        with pytest.raises(mcp_oauth.OAuthError) as e:
            mcp_oauth._token_request(base + "/token", {"grant_type": "refresh_token"})
        text = str(e.value)
        assert "HTTP 400" in text and len(text) < 400 and "\n" not in text
    finally:
        httpd.shutdown()


# ── a11: адресът за вход в терминала е един ред ──────────────────────────────

def test_the_login_address_is_printed_as_one_line(monkeypatch) -> None:
    # Rich режеше ~300-знаковия адрес с истински нови редове: Ctrl/Cmd-клик
    # отваряше адрес без code_challenge и state.
    from rich.console import Console

    import genesis_terminal_agent as gta
    buf = io.StringIO()
    monkeypatch.setattr(gta, "console", Console(file=buf, width=80))
    url = ("http://127.0.0.1:35791/authorize?response_type=code&client_id=client-8ecb373d"
           "&redirect_uri=http%3A%2F%2F127.0.0.1%3A33535%2Fcallback&code_challenge=" + "x" * 43 +
           "&code_challenge_method=S256&state=" + "s" * 32 + "&resource=http%3A%2F%2F127.0.0.1%2Fmcp")
    gta._command_out("Вход в браузъра (ако не се отвори, копирай адреса):\n" + url)
    assert url in buf.getvalue().splitlines()
    # Обикновеният текст се пренася по думи както досега.
    buf.seek(0)
    buf.truncate()
    gta._command_out("дума " * 40)
    assert all(len(line) <= 80 for line in buf.getvalue().splitlines())


# ── a13: /mcp logout на вход, който е само в паметта ─────────────────────────

def test_logout_of_a_login_kept_only_in_memory_says_forgotten(oauth, monkeypatch, tmp_path) -> None:
    url, _ = oauth()

    def full_disk(_data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(mcp_oauth, "_save_all", full_disk)
    _login(url)
    assert mcp_oauth.access_token(url)
    seen: list[str] = []
    chat_commands.handle("/mcp logout linear", messages=deque(), workspace=tmp_path,
                         out=seen.append, ask=lambda q: "")
    assert any("е забравен" in s for s in seen), seen
    assert mcp_oauth.access_token(url) == ""
