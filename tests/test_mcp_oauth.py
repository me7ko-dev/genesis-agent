"""OAuth вход за MCP сървъри по HTTP (2026-10-09): сървър, който иска вход,
досега даваше само „HTTP 401 — провери headers (токена) в mcp.json“."""
from __future__ import annotations

import json
import os
import sys
import time
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import genesis_skills as gs
from genesis_agent import chat_commands, mcp_client, mcp_oauth, paths


def _browser(url: str) -> None:
    """Операторът в браузъра: влиза и сървърът го праща обратно към Genesis."""
    import requests
    requests.get(url, timeout=10, allow_redirects=True)


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    import fake_oauth_mcp
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(gs, "_WORKSPACE", tmp_path)
    started = []

    def start(headers=None, **kw):
        url, httpd, state = fake_oauth_mcp.serve(**kw)
        started.append(httpd)
        spec = {"type": "http", "url": url, **({"headers": headers} if headers else {})}
        (Path(paths.GENESIS_HOME) / "mcp.json").write_text(
            json.dumps({"mcpServers": {"linear": spec}}), encoding="utf-8")
        return url, state
    yield start
    mcp_client.stop_all()
    for h in started:
        h.shutdown()


def _login(url: str) -> dict:
    return mcp_oauth.login(url, open_browser=_browser, say=lambda _t: None, timeout=20)


def test_without_a_login_the_server_says_how_to_log_in(oauth) -> None:
    oauth()
    lines = mcp_client.start_all()
    assert any("нужен е вход — /mcp login linear" in ln for ln in lines), lines


def test_login_in_the_browser_then_the_tools_work(oauth) -> None:
    url, state = oauth()
    entry = _login(url)
    assert entry["access_token"] in state["tokens"]
    assert state["resource_seen"] == [url]                  # RFC 8707: токенът е за този сървър
    assert any("linear: 1 инструмента" in ln for ln in mcp_client.start_all())
    assert gs.dispatch_tool_call("mcp__linear__whoami", {}) == "[MCP linear.whoami] влязъл"
    if os.name != "nt":
        assert (Path(paths.GENESIS_HOME) / "mcp_tokens.json").stat().st_mode & 0o077 == 0


def test_an_expired_token_is_refreshed_without_asking(oauth) -> None:
    url, _ = oauth()
    old = _login(url)["access_token"]
    data = json.loads((Path(paths.GENESIS_HOME) / "mcp_tokens.json").read_text(encoding="utf-8"))
    for entry in data.values():
        entry["expires_at"] = time.time() - 10
    (Path(paths.GENESIS_HOME) / "mcp_tokens.json").write_text(json.dumps(data), encoding="utf-8")
    assert any("linear: 1 инструмента" in ln for ln in mcp_client.start_all())
    assert mcp_oauth.saved(url)["access_token"] != old


def test_a_revoked_token_is_refreshed_once_on_401(oauth) -> None:
    url, state = oauth()
    token = _login(url)["access_token"]
    state["tokens"].discard(token)                           # сървърът го е отнел
    assert any("linear: 1 инструмента" in ln for ln in mcp_client.start_all())
    assert state["refused"] == 1


def test_a_dead_refresh_token_asks_for_a_new_login(oauth) -> None:
    url, state = oauth()
    _login(url)
    state["tokens"].clear()
    state["refresh"].clear()
    assert any("нужен е вход" in ln for ln in mcp_client.start_all())


def test_an_explicit_header_wins_over_the_login(oauth) -> None:
    url, _ = oauth(headers={"Authorization": "Bearer from-mcp-json"})
    _login(url)
    lines = mcp_client.start_all()
    assert not any("1 инструмента" in ln for ln in lines)   # headers на оператора — без подмяна


def test_an_http_login_page_off_this_machine_is_refused(oauth) -> None:
    url, _ = oauth(insecure_authorize=True)
    with pytest.raises(mcp_oauth.OAuthError, match="https"):
        _login(url)


def test_without_registration_it_says_to_use_a_header(oauth) -> None:
    url, _ = oauth(registration=False)
    with pytest.raises(mcp_oauth.OAuthError, match="headers"):
        _login(url)


def test_a_callback_with_the_wrong_state_is_ignored(oauth, monkeypatch) -> None:
    url, _ = oauth()

    def forged(auth_url: str) -> None:
        from urllib.parse import parse_qs, urlsplit

        import requests
        q = {k: v[0] for k, v in parse_qs(urlsplit(auth_url).query).items()}
        requests.get(q["redirect_uri"] + "?code=stolen&state=wrong", timeout=5)
    with pytest.raises(mcp_oauth.OAuthError, match="навреме"):
        mcp_oauth.login(url, open_browser=forged, say=lambda _t: None, timeout=2)
    assert mcp_oauth.saved(url) is None


def test_mcp_login_and_logout_from_the_chat(oauth, monkeypatch, tmp_path) -> None:
    url, _ = oauth()
    monkeypatch.setattr(mcp_oauth, "_open", _browser)
    monkeypatch.setattr(mcp_oauth, "no_browser", lambda: False)   # CI е без DISPLAY
    seen: list[str] = []
    chat_commands.handle("/mcp login linear", messages=deque(), workspace=tmp_path,
                         out=seen.append, ask=lambda q: "")
    assert any("✓ Вход в linear" in s for s in seen), seen
    assert any("linear: 1 инструмента" in s for s in seen)
    chat_commands.handle("/mcp logout linear", messages=deque(), workspace=tmp_path,
                         out=seen.append, ask=lambda q: "")
    assert mcp_oauth.saved(url) is None


def test_mcp_login_is_refused_from_the_phone(tmp_path) -> None:
    pytest.importorskip("cryptography")
    from genesis_agent import remote_server as rs

    class UI:
        def __init__(self) -> None:
            self.warns: list[str] = []

        def warn(self, t: str) -> None:
            self.warns.append(t)

        def info(self, t: str) -> None:
            pass
    ui = UI()
    _, prompt = rs.phone_command("/mcp login linear", ui, rs.RemoteSession(lambda t, u: None),
                                 messages=deque(), workspace=tmp_path)
    assert prompt is None and "терминала" in ui.warns[0]


# ── одит 2026-10-09 ──────────────────────────────────────────────────────────

def _tiny_server(handler_get=None, handler_post=None):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, obj, extra=None):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            handler_get(self)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            handler_post(self)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_a_server_cannot_ask_for_another_servers_token(oauth) -> None:
    # Зъл сървър сочи жертвата като `resource`: операторът вижда истинската
    # страница за вход, а токенът отиваше при злия (възпроизведено).
    victim, _ = oauth()
    victim_base = victim.rsplit("/", 1)[0]
    holder: dict = {}

    def get(h):
        if h.path.startswith("/.well-known/oauth-protected-resource"):
            return h._send(200, {"resource": victim, "authorization_servers": [victim_base]})
        return h._send(404, {})

    def post(h):
        holder.setdefault("auth", []).append(h.headers.get("Authorization", ""))
        meta = holder["base"] + "/.well-known/oauth-protected-resource/mcp"
        h._send(401, {}, {"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'})
    httpd, base = _tiny_server(get, post)
    holder["base"] = base
    try:
        with pytest.raises(mcp_oauth.OAuthError, match="отказано"):
            _login(base + "/mcp")
        assert mcp_oauth.saved(base + "/mcp") is None
    finally:
        httpd.shutdown()


def test_metadata_for_another_issuer_is_refused(oauth) -> None:
    oauth()

    def get(h):
        if "oauth-protected-resource" in h.path:
            return h._send(200, {"resource": holder["base"] + "/mcp",
                                 "authorization_servers": [holder["base"]]})
        if "oauth-authorization-server" in h.path:
            return h._send(200, {"issuer": "https://attacker.example",
                                 "authorization_endpoint": holder["base"] + "/a",
                                 "token_endpoint": holder["base"] + "/t",
                                 "registration_endpoint": holder["base"] + "/r"})
        return h._send(404, {})
    holder: dict = {}
    httpd, base = _tiny_server(get, lambda h: h._send(401, {}))
    holder["base"] = base
    try:
        with pytest.raises(mcp_oauth.OAuthError, match="attacker"):
            mcp_oauth.discover(base + "/mcp")
    finally:
        httpd.shutdown()


def test_openid_discovery_under_the_issuer_path_works(oauth) -> None:
    holder: dict = {}

    def get(h):
        if h.path == "/.well-known/oauth-protected-resource/mcp":
            return h._send(200, {"resource": holder["base"] + "/mcp",
                                 "authorization_servers": [holder["base"] + "/realms/acme"]})
        if h.path == "/realms/acme/.well-known/openid-configuration":
            b = holder["base"] + "/realms/acme"
            return h._send(200, {"issuer": b, "authorization_endpoint": b + "/auth",
                                 "token_endpoint": b + "/token"})
        return h._send(404, {})
    httpd, base = _tiny_server(get, lambda h: h._send(401, {}))
    holder["base"] = base
    try:
        assert mcp_oauth.discover(base + "/mcp")["token_endpoint"].endswith("/realms/acme/token")
    finally:
        httpd.shutdown()


def test_an_idle_connection_does_not_hang_the_login(oauth) -> None:
    import socket
    import threading
    url, _ = oauth()
    idle: list[socket.socket] = []

    def browser(auth_url: str) -> None:
        from urllib.parse import parse_qs, urlsplit
        q = {k: v[0] for k, v in parse_qs(urlsplit(auth_url).query).items()}
        port = urlsplit(q["redirect_uri"]).port
        idle.append(socket.create_connection(("127.0.0.1", port)))   # preconnect, мълчи
        threading.Timer(0.5, _browser, args=(auth_url,)).start()
    start = time.monotonic()
    entry = mcp_oauth.login(url, open_browser=browser, say=lambda _t: None, timeout=30)
    assert entry["access_token"] and time.monotonic() - start < 20
    for s in idle:
        s.close()


def test_two_refreshes_at_once_spend_the_refresh_token_once(oauth) -> None:
    import threading
    url, state = oauth()
    old = _login(url)["access_token"]
    state["tokens"].discard(old)                             # отказан и от двамата
    got: list[str | None] = []
    threads = [threading.Thread(target=lambda: got.append(mcp_oauth.refresh(url, stale=old)))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert all(got) and len(set(got)) == 1                   # един нов токен за всички
    assert got[0] in state["tokens"]


def test_a_token_that_cannot_be_saved_still_works_in_this_process(oauth, monkeypatch) -> None:
    url, _ = oauth()
    _login(url)
    data = json.loads((Path(paths.GENESIS_HOME) / "mcp_tokens.json").read_text(encoding="utf-8"))
    for entry in data.values():
        entry["expires_at"] = time.time() - 10
    (Path(paths.GENESIS_HOME) / "mcp_tokens.json").write_text(json.dumps(data), encoding="utf-8")

    def full_disk(_data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(mcp_oauth, "_save_all", full_disk)
    assert any("linear: 1 инструмента" in ln for ln in mcp_client.start_all())
    assert gs.dispatch_tool_call("mcp__linear__whoami", {}) == "[MCP linear.whoami] влязъл"


def test_a_short_lived_token_is_not_refreshed_on_every_call(oauth) -> None:
    url, state = oauth()
    state["expires_in"] = 30
    _login(url)
    tokens = {mcp_oauth.access_token(url) for _ in range(5)}
    assert len(tokens) == 1


def test_a_static_api_key_header_still_says_check_the_headers(oauth) -> None:
    oauth(headers={"X-API-Key": "wrong"})
    lines = mcp_client.start_all()
    assert any("401" in ln and "провери headers" in ln for ln in lines), lines
    assert not any("нужен е вход" in ln for ln in lines)


def test_the_token_file_is_a_secret_for_the_sandbox() -> None:
    from genesis_agent import sandbox
    assert sandbox.assess_command("cat ~/.genesis/mcp_tokens.json").level.name == "CONFIRM"
    assert sandbox.sensitive_path_reason(Path(paths.GENESIS_HOME) / "mcp_tokens.json")
    assert sandbox.assess_command("cat ~/.genesis/checkpoints/abc/objects/x").level.name == "CONFIRM"


# ── втори кръг одит 2026-10-09 ───────────────────────────────────────────────

def test_metadata_at_the_root_of_the_host_is_accepted(oauth) -> None:
    url, state = oauth(root_resource=True)
    _login(url)
    assert state["resource_seen"] == [url.rsplit("/", 1)[0]]     # каквото сървърът иска
    assert any("linear: 1 инструмента" in ln for ln in mcp_client.start_all())


def test_a_non_auth_header_does_not_turn_off_oauth(oauth) -> None:
    url, state = oauth(headers={"X-Tenant": "acme"})
    token = _login(url)["access_token"]
    state["tokens"].discard(token)                                # отнет → подновяване
    assert any("linear: 1 инструмента" in ln for ln in mcp_client.start_all())


def test_the_token_file_lock_is_held_across_processes(tmp_path) -> None:
    import subprocess
    import textwrap
    script = textwrap.dedent("""
        import sys, time
        from pathlib import Path
        from genesis_agent import mcp_oauth, paths
        paths.GENESIS_HOME = Path(sys.argv[1])
        with mcp_oauth._FileLock():
            print("held", flush=True)
            time.sleep(1.5)
    """)
    child = subprocess.Popen([sys.executable, "-c", script, str(paths.GENESIS_HOME)],
                             stdout=subprocess.PIPE, text=True,
                             cwd=str(Path(__file__).resolve().parents[1]))
    assert child.stdout is not None and child.stdout.readline().strip() == "held"
    # Дълго държана ключалка не се „краде“ по възраст, докато държащият е жив.
    lock = mcp_oauth._store_path().with_suffix(".lock")
    old = time.time() - 600
    os.utime(lock, (old, old))
    start = time.monotonic()
    with mcp_oauth._FileLock():
        waited = time.monotonic() - start
    child.wait(10)
    assert waited > 0.5                                           # чакахме другия процес


def test_ctrl_c_while_waiting_for_the_lock_does_not_freeze_the_process(monkeypatch) -> None:
    def interrupted(_fh):
        raise KeyboardInterrupt
    monkeypatch.setattr(mcp_oauth, "_lock_file", interrupted)
    with pytest.raises(KeyboardInterrupt):
        mcp_oauth.forget("http://127.0.0.1:1/mcp")
    assert not mcp_oauth._lock.locked()


# ── вход по SSH без браузър (2026-10-09) ─────────────────────────────────────

def _browser_elsewhere(auth_url: str) -> str:
    """Браузър на друг компютър: влиза, сървърът го праща към 127.0.0.1, който
    там не отговаря — операторът копира адреса от лентата."""
    import requests
    resp = requests.get(auth_url, timeout=10, allow_redirects=False)
    return resp.headers["Location"]


def test_login_by_pasting_the_address_over_ssh(oauth) -> None:
    url, state = oauth()
    asked: list[str] = []
    shown: list[str] = []

    def paste(question: str) -> str:
        asked.append(question)
        auth_url = next(s for s in shown if "/authorize?" in s).split("\n")[-1]
        return _browser_elsewhere(auth_url)
    entry = mcp_oauth.login(url, say=shown.append, paste=paste, timeout=5)
    assert asked and entry["access_token"] in state["tokens"]


def test_a_pasted_address_from_another_login_is_refused(oauth) -> None:
    url, _ = oauth()
    with pytest.raises(mcp_oauth.OAuthError, match="state"):
        mcp_oauth.login(url, say=lambda _t: None,
                        paste=lambda _q: "http://127.0.0.1:1/callback?code=x&state=forged")


def test_mcp_login_paste_from_the_chat(oauth, tmp_path) -> None:
    oauth()
    seen: list[str] = []

    def ask(_q: str) -> str:
        auth_url = next(s for s in seen if "/authorize?" in s).split("\n")[-1]
        return _browser_elsewhere(auth_url)
    chat_commands.handle("/mcp login linear --paste", messages=deque(), workspace=tmp_path,
                         out=seen.append, ask=ask)
    assert any("✓ Вход в linear" in s for s in seen), seen


def test_ssh_means_no_browser(monkeypatch) -> None:
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 5000 10.0.0.1 22")
    assert mcp_oauth.no_browser()


def test_an_empty_paste_says_what_is_missing(oauth) -> None:
    url, _ = oauth()
    with pytest.raises(mcp_oauth.OAuthError, match="поставен"):
        mcp_oauth.login(url, say=lambda _t: None, paste=lambda _q: "")
