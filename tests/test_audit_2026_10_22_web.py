"""Одит 2026-10-22, подсистема „web“ (web_fetch.py, browser.py).

Като в test_web_fetch.py: истински локален HTTP сървър; имена, завършващи на
.public (и .бг → xn--90ae), се разрешават до публичен адрес (93.184.216.34),
а сокетът за него се насочва към локалния сървър (web_fetch._connect) —
пазачът вижда точно това, което вижда навън. Playwright не е инсталиран тук:
в теста на BROWSE е подменена само страницата, browser.navigate и
genesis_skills.dispatch_tool_call са истинските."""
from __future__ import annotations

import datetime
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

import genesis_skills as gs
from genesis_agent import browser
from genesis_agent import web_fetch as wf

PUBLIC = "93.184.216.34"
_real_getaddrinfo = socket.getaddrinfo


def _resolve(host: Any, p: Any, *a: Any, **k: Any) -> Any:
    name = str(host)
    if name.endswith((".public", ".xn--90ae")):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, p))]
    if name.endswith(".internal"):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", p))]
    return _real_getaddrinfo(host, p, *a, **k)


@pytest.fixture
def site(monkeypatch):
    pages: dict[str, Any] = {}
    hits: list[str] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            page = pages.get(self.path, (404, b"", {}))
            if callable(page):
                return page(self)
            status, body, headers = page
            self.send_response(status)
            for k, v in {"Content-Type": "text/html; charset=utf-8", **headers}.items():
                if v:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    monkeypatch.setattr(wf.socket, "getaddrinfo", _resolve)
    real_connect = wf._connect

    def connect(sock, sockaddr):
        real_connect(sock, ("127.0.0.1", port) if sockaddr[0] == PUBLIC else sockaddr)
    monkeypatch.setattr(wf, "_connect", connect)
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)

    def add(path, body=b"", status=200, **headers):
        pages[path] = (status, body, {k.replace("_", "-"): v for k, v in headers.items()})
    add.pages = pages
    add.hits = hits
    yield add
    httpd.shutdown()


def _tls_context(tmp_path, host: str, monkeypatch) -> ssl.SSLContext:
    """Самоподписан сертификат за host; requests му вярва през REQUESTS_CA_BUNDLE."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, True, False, False, False),
                           critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                           critical=False)
            .sign(key, hashes.SHA256()))
    cert_pem, key_pem = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_pem.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert_pem))
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_pem), str(key_pem))
    return ctx


class _FakePage:
    """Само обектът на Playwright е подменен: goto помни адреса, а lands_on
    казва къде „пренасочва“ страницата (redirect, meta refresh, JS)."""

    def __init__(self, lands_on: dict[str, str]) -> None:
        self.url = ""
        self.visited: list[str] = []
        self.lands_on = lands_on

    def goto(self, url: str, wait_until: str | None = None) -> None:
        self.visited.append(url)
        self.url = self.lands_on.get(url, url)

    def title(self) -> str:
        return "meta-data"

    def inner_text(self, _sel: str) -> str:
        return "AccessKeyId SECRET" if "169.254" in self.url else "публичен текст"

    def evaluate(self, *_a: Any) -> list:
        return []


class _FakeRoute:
    def __init__(self, url: str) -> None:
        self.request = type("Req", (), {"url": url})()
        self.done = ""

    def abort(self, _code: str = "") -> None:
        self.done = "abort"

    def continue_(self) -> None:
        self.done = "continue"


def test_browse_applies_the_web_fetch_address_policy(monkeypatch) -> None:
    """BROWSE е SAFE и не пита — а отваряше metadata адреса, който WEB_FETCH
    отказва, и връщаше текста му на модела."""
    monkeypatch.setattr(socket, "getaddrinfo", _resolve)
    page = _FakePage({"http://evil.public/": "http://169.254.169.254/latest/meta-data/"})
    monkeypatch.setattr(browser, "_page", page)
    monkeypatch.setattr(browser, "_allow_loopback", False)
    monkeypatch.setattr(browser, "_last_elements", [])
    for url in ("http://169.254.169.254/latest/meta-data/iam/security-credentials/role",
                "http://192.168.0.1/", "http://router.internal/", "http://100.100.100.200/",
                "http://10.0.0.1\\@docs.public/"):
        out = gs.dispatch_tool_call("BROWSE", {"url": url})
        assert "❌" in out and "SECRET" not in out, (url, out)
    assert page.visited == []
    # Публичен адрес, който стига до вътрешен (redirect / meta refresh / JS):
    # текстът на крайната страница не стига до модела.
    out = gs.dispatch_tool_call("BROWSE", {"url": "http://evil.public/"})
    assert "SECRET" not in out and "вътрешна мрежа" in out
    # localhost, когато е поискан изрично — както при WEB_FETCH.
    assert "публичен текст" in browser.navigate("http://127.0.0.1:8000/")
    # Заявките, които самата страница прави, минават през същото правило.
    assert "публичен текст" in browser.navigate("http://docs.public/")
    verdicts = {}
    for url in ("http://169.254.169.254/latest/", "http://127.0.0.1:2375/containers",
                "http://router.internal/", "http://docs.public/app.js", "data:text/plain,x"):
        route = _FakeRoute(url)
        browser._route_guard(route)
        verdicts[url] = route.done
    assert verdicts == {"http://169.254.169.254/latest/": "abort",
                        "http://127.0.0.1:2375/containers": "abort",
                        "http://router.internal/": "abort",
                        "http://docs.public/app.js": "continue",
                        "data:text/plain,x": "continue"}


def test_the_total_deadline_holds_inside_a_slow_read(site, monkeypatch, tmp_path) -> None:
    """Срокът се проверяваше само между парчетата от 64 KB: тяло с
    Content-Length по 100 байта на 0.05 s или заглавки по байт държаха
    инструмента колкото сървърът искаше (мерено: 33 s и 22.6 s при срок 3 s)."""
    monkeypatch.setattr(wf, "_TIMEOUT", 1)

    def body_drip(h):
        h.send_response(200)
        h.send_header("Content-Type", "text/plain")
        h.send_header("Content-Length", "1000000")
        h.end_headers()
        try:
            for _ in range(120):
                h.wfile.write(b"x" * 100)
                h.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass

    def head_drip(h):
        try:
            for c in b"HTTP/1.0 200 OK\r\nX-Pad: " + b"a" * 120 + b"\r\nContent-Length: 2\r\n\r\nok":
                h.wfile.write(bytes([c]))
                h.wfile.flush()
                time.sleep(0.05)
        except OSError:
            pass

    site.pages["/body"] = body_drip
    site.pages["/head"] = head_drip
    for path in ("/body", "/head"):
        start = time.monotonic()
        out = wf.web_fetch(f"http://slow.public{path}")
        took = time.monotonic() - start
        assert "над 1 s" in out and took < 3, (path, round(took, 1), out[:120])

    # Същото през TLS: след connect urllib3 държи обвития сокет, а суровият е
    # откачен (fd -1); при „Connection: close“ conn.sock става None. Първата
    # версия на поправката затваряше само тези двата и чакаше целия drip.
    class Drip(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body_drip(self)

    tls = ThreadingHTTPServer(("127.0.0.1", 0), Drip)
    tls.daemon_threads = True
    tls.socket = _tls_context(tmp_path, "slow.public", monkeypatch).wrap_socket(
        tls.socket, server_side=True)
    threading.Thread(target=tls.serve_forever, daemon=True).start()
    real_connect = wf._connect
    monkeypatch.setattr(wf, "_connect", lambda sock, sockaddr: real_connect(
        sock, ("127.0.0.1", tls.server_address[1])))
    try:
        start = time.monotonic()
        out = wf.web_fetch("https://slow.public/body")
        took = time.monotonic() - start
    finally:
        tls.shutdown()
    assert "над 1 s" in out and took < 3, ("https", round(took, 1), out[:200])


def test_idn_domains_are_fetched(site) -> None:
    """urlparse дава хоста в Unicode, urllib3 — в IDNA (xn--…): всеки .бг адрес
    излизаше „двусмислен хост“, без да се отвори."""
    site("/", "<h1>Начало</h1>".encode())
    assert "# Начало" in wf.web_fetch("http://пример.бг/")
    assert "# Начало" in wf.web_fetch("http://ПРИМЕР.public/")
    # Пазачът на адреса остава — гледа разрешения адрес, не името.
    assert "вътрешна мрежа" in wf.web_fetch("http://рутер.internal/")


def test_a_quoted_charset_is_honoured(site) -> None:
    body = "<title>Тест</title><h1>Здравей, свят</h1><p>Кирилица</p>".encode("cp1251")
    site("/q", body, Content_Type='text/html; charset="windows-1251"')
    site("/s", body, Content_Type="text/html; charset = windows-1251")
    for path in ("/q", "/s"):
        out = wf.web_fetch(f"http://old.public{path}")
        assert "# Тест" in out and "# Здравей, свят" in out and "Кирилица" in out, out


def test_textual_formats_beyond_text_star(site) -> None:
    for i, ctype in enumerate(("application/ld+json", "application/atom+xml",
                               "application/rss+xml", "application/javascript",
                               "application/yaml")):
        site(f"/t{i}", f"payload-{i}".encode(), Content_Type=ctype)
        out = wf.web_fetch(f"http://feeds.public/t{i}")
        assert f"payload-{i}" in out and "не е текст" not in out, (ctype, out)
    site("/notype", b"<h1>hi</h1>", Content_Type="")
    assert "hi" in wf.web_fetch("http://feeds.public/notype")
    # Двоичното остава отказано — и с тип, и без.
    site("/zip", b"PK\x03\x04\x00\x00", Content_Type="application/zip")
    site("/blob", b"\x89PNG\r\n\x1a\n\x00\x00\x00", Content_Type="")
    site("/octet", b"\x00\x01\x02binary", Content_Type="application/octet-stream")
    for path in ("/zip", "/blob", "/octet"):
        assert "не е текст" in wf.web_fetch(f"http://feeds.public{path}"), path


def test_links_wrapping_code_and_definition_lists() -> None:
    _, text = wf.html_to_text(
        '<ul><li><a href="/api"><code>api.get()</code></a> - fetch</li></ul>', "https://d.example/")
    assert "[`api.get()`](https://d.example/api) - fetch" in text and "``" not in text
    _, text = wf.html_to_text(
        '<dl class="field-list"><dt>Return type</dt><dd><p><a href="api.html#requests.Response">'
        '<code class="xref"><span class="pre">Response</span></code></a></p></dd></dl>',
        "https://requests.readthedocs.io/en/latest/")
    assert "Return type\n" in text and \
        "[`Response`](https://requests.readthedocs.io/en/latest/api.html#requests.Response)" in text
    _, text = wf.html_to_text(
        "<dl><dt>timeout</dt><dd>Seconds to wait.</dd><dt>retries</dt><dd>Count.</dd></dl>")
    assert "timeoutSeconds" not in text and "wait.retries" not in text
    assert [ln.strip() for ln in text.splitlines() if ln.strip()] == \
        ["timeout", "Seconds to wait.", "retries", "Count."]
