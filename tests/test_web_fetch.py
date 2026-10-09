"""WEB_FETCH — a page by its address as readable text, never an internal one."""
from __future__ import annotations

import socket

import pytest

import genesis_skills as gs
from genesis_agent import web_fetch as wf


class _Resp:
    def __init__(self, status=200, body=b"", ctype="text/html; charset=utf-8", location=""):
        self.status_code = status
        self.headers = {"content-type": ctype, **({"location": location} if location else {})}
        self._body = body
        self.encoding = "utf-8"

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308)

    def iter_content(self, n):
        yield self._body

    def close(self):
        pass


@pytest.fixture
def net(monkeypatch):
    """DNS: *.public → 93.184.216.34, *.internal → 10.0.0.5; requests.get по сценарий."""
    def resolve(host, port, *a, **k):
        ip = "10.0.0.5" if host.endswith(".internal") else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(wf.socket, "getaddrinfo", resolve)
    pages: dict[str, _Resp] = {}
    import requests
    monkeypatch.setattr(requests, "get", lambda url, **k: pages[url])
    return pages


def test_the_page_keeps_its_structure(net) -> None:
    net["https://docs.public/x"] = _Resp(body=(
        "<html><head><title>API</title><script>evil()</script></head><body><nav>menu</nav>"
        "<h1>Install</h1><p>Run <code>pip install x</code>.</p><ul><li>one</li><li>two</li></ul>"
        "<pre>x = 1\n  y = 2</pre><a href='/more'>More docs</a><footer>©</footer></body></html>"
    ).encode())
    out = wf.web_fetch("https://docs.public/x")
    assert "# API" in out and "# Install" in out and "`pip install x`" in out
    assert "- one" in out and "x = 1\n  y = 2" in out
    assert "[More docs](https://docs.public/more)" in out
    assert "evil" not in out and "menu" not in out and "©" not in out


@pytest.mark.parametrize("url", ["http://169.254.169.254/latest/meta-data/", "http://10.1.2.3/",
                                 "http://192.168.0.1/admin", "https://router.internal/", "file:///etc/passwd"])
def test_internal_addresses_are_refused(net, url) -> None:
    assert "❌" in wf.web_fetch(url)


def test_a_redirect_into_the_internal_network_is_refused(net) -> None:
    net["https://short.public/a"] = _Resp(status=302, location="http://169.254.169.254/latest/")
    out = wf.web_fetch("https://short.public/a")
    assert "вътрешна мрежа" in out


def test_loopback_is_allowed_for_dev_servers(net) -> None:
    net["http://127.0.0.1:3000/"] = _Resp(body=b"<h1>My app</h1>")
    assert "# My app" in wf.web_fetch("http://127.0.0.1:3000/")


def test_binary_is_refused_and_long_text_is_cut(net) -> None:
    net["https://a.public/f.zip"] = _Resp(body=b"PK\x03\x04", ctype="application/zip")
    assert "не е текст" in wf.web_fetch("https://a.public/f.zip")
    net["https://a.public/big"] = _Resp(body=("<p>" + "word " * 20000 + "</p>").encode())
    assert "отрязано" in wf.web_fetch("https://a.public/big")


def test_both_tool_paths(net) -> None:
    net["https://a.public/"] = _Resp(body=b"<h2>Hello</h2>")
    assert "## Hello" in gs.dispatch_tool_call("WEB_FETCH", {"url": "https://a.public/"})
    assert "## Hello" in gs.parse_and_execute_tools("[WEB_FETCH: https://a.public/]")[0]
