"""WEB_FETCH — a page by its address as readable text, never an internal one.

A real local HTTP server serves the pages. Names ending in .public resolve to a
public-looking address (93.184.216.34) and the socket for it is pointed at the
local server (web_fetch._connect) — so the guard sees exactly what it sees in
the wild."""
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import genesis_skills as gs
from genesis_agent import web_fetch as wf

PUBLIC = "93.184.216.34"


@pytest.fixture
def site(monkeypatch):
    pages: dict[str, tuple[int, bytes, dict[str, str]]] = {}
    hits: list[str] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            status, body, headers = pages.get(self.path, (404, b"", {}))
            self.send_response(status)
            for k, v in {"Content-Type": "text/html; charset=utf-8", **headers}.items():
                if v:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    real_getaddrinfo = socket.getaddrinfo

    def resolve(host, p, *a, **k):
        if str(host).endswith(".public"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, p))]
        if str(host).endswith(".internal"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", p))]
        return real_getaddrinfo(host, p, *a, **k)
    monkeypatch.setattr(wf.socket, "getaddrinfo", resolve)
    real_connect = wf._connect

    def connect(sock, sockaddr):
        real_connect(sock, ("127.0.0.1", port) if sockaddr[0] == PUBLIC else sockaddr)
    monkeypatch.setattr(wf, "_connect", connect)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)

    def add(path, body=b"", status=200, **headers):
        pages[path] = (status, body, {k.replace("_", "-"): v for k, v in headers.items()})
    add.port = port
    add.hits = hits
    yield add
    httpd.shutdown()


def test_the_page_keeps_its_structure(site) -> None:
    site("/x", (
        "<html><head><title>API</title><script>evil()</script></head><body><nav>menu</nav>"
        "<h1>Install</h1><p>Run <code>pip install x</code>.</p><ul><li>one</li><li>two</li></ul>"
        "<pre>x = 1\n  y = 2</pre><a href='/more'>More docs</a><footer>©</footer></body></html>"
    ).encode())
    out = wf.web_fetch("http://docs.public/x")
    assert "# API" in out and "# Install" in out and "`pip install x`" in out
    assert "- one" in out and "x = 1\n  y = 2" in out
    assert "[More docs](http://docs.public/more)" in out
    assert "evil" not in out and "menu" not in out and "©" not in out


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "http://10.1.2.3/",
                                 "http://192.168.0.1/admin", "http://router.internal/",
                                 "http://100.100.100.200/latest/meta-data/", "http://100.64.0.1/",
                                 "file:///etc/passwd"])
def test_internal_addresses_are_refused(site, url) -> None:
    assert "❌" in wf.web_fetch(url)


@pytest.mark.parametrize("url", ["http://10.0.0.1\\@docs.public/x", "http://user@docs.public/x"])
def test_ambiguous_hosts_are_refused(site, url) -> None:
    """Одит 2026-10-09: urlparse виждаше docs.public, urllib3 се свързваше с 10.0.0.1."""
    out = wf.web_fetch(url)
    assert "❌" in out and site.hits == []


def test_a_redirect_into_the_internal_network_is_refused(site) -> None:
    site("/a", status=302, Location="http://169.254.169.254/latest/")
    site("/b", status=302, Location="http://10.0.0.1\\@docs.public/../latest/")
    assert "вътрешна мрежа" in wf.web_fetch("http://short.public/a")
    assert "❌" in wf.web_fetch("http://short.public/b")


def test_a_public_page_cannot_send_the_agent_to_localhost(site) -> None:
    site("/secrets", b'{"kind": "SecretList"}', Content_Type="application/json")
    site("/go", status=302, Location=f"http://127.0.0.1:{site.port}/secrets")
    out = wf.web_fetch("http://evil.public/go")
    assert "SecretList" not in out and "❌" in out


def test_the_checked_address_is_the_one_connected_to(site, monkeypatch) -> None:
    """DNS rebinding (одит 2026-10-09): първото питане — публичен адрес, второто —
    вътрешен. Името се разрешава ВЕДНЪЖ и връзката е към проверения адрес —
    второ питане, което да върне вътрешния, няма."""
    answers = iter([PUBLIC, "10.0.0.5", "10.0.0.5"])
    asked: list[str] = []

    def flip(host, p, *a, **k):
        asked.append(str(host))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), p))]
    monkeypatch.setattr(wf.socket, "getaddrinfo", flip)
    connected: list[str] = []
    seam = wf._connect

    def record(sock, sockaddr):
        connected.append(sockaddr[0])
        seam(sock, sockaddr)
    monkeypatch.setattr(wf, "_connect", record)
    site("/", b"<p>page</p>")
    wf.web_fetch("http://rebind.public/")
    assert asked == ["rebind.public"] and connected == [PUBLIC]


def test_loopback_is_allowed_when_asked_for(site) -> None:
    site("/", b"<h1>My app</h1>")
    assert "# My app" in wf.web_fetch(f"http://127.0.0.1:{site.port}/")


def test_utf8_without_charset_and_upper_case_type(site) -> None:
    site("/bg", "<h1>Здравей</h1>".encode(), Content_Type="TEXT/HTML")
    assert "# Здравей" in wf.web_fetch("http://bg.public/bg")


def test_binary_is_refused_and_long_text_is_cut(site) -> None:
    site("/f.zip", b"PK\x03\x04", Content_Type="application/zip")
    assert "не е текст" in wf.web_fetch("http://a.public/f.zip")
    site("/big", ("<p>" + "word " * 20000 + "</p>").encode())
    assert "отрязано" in wf.web_fetch("http://a.public/big")


def test_bad_addresses_never_raise(site) -> None:
    for url in ("http://a.public:99999/", "http://[::1/", "nonsense"):
        assert "❌" in wf.web_fetch(url)


def test_html_edge_cases() -> None:
    assert wf.html_to_text("<form><h1>Body</h1></form>")[1] == "# Body"
    _, text = wf.html_to_text("<p><a href='/h'>Home</p><h2>Install</h2>", "https://x/")
    assert "[Home](https://x/h)" in text and "## Install" in text
    assert "A | B" in wf.html_to_text("<table><tr><th>A</th><th>B</th></tr></table>")[1]


def test_both_tool_paths(site) -> None:
    site("/", b"<h2>Hello</h2>")
    assert "## Hello" in gs.dispatch_tool_call("WEB_FETCH", {"url": "http://a.public/"})
    assert "## Hello" in gs.parse_and_execute_tools("[WEB_FETCH: http://a.public/]")[0]
