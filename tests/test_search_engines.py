"""genesis_agent.search_engines — много търсачки паралелно (2026-11-17).

Мерено на лаптопа: DuckDuckGo Lite — CAPTCHA на кирилица, Bing — чужди
резултати, Brave — 429; RESEARCH с една търсачка: 2/20. Тук: сливането по
адрес, почивката на отказала търсачка, браузърът само при малко резултати и
разчитането на DuckDuckGo html. Мрежа няма — функциите на търсачките са
подменени (conftest спира ключовете, браузъра и POST-а)."""
from __future__ import annotations

import json

import pytest

from genesis_agent import search_engines as se

# Истинският `_key` — conftest (_no_real_search) го подменя с празно за всеки тест.
_REAL_KEY = se._key


def _row(url: str, title: str = "t", snippet: str = "") -> dict:
    return {"title": title, "url": url, "snippet": snippet}


def test_norm_url_merges_the_same_page() -> None:
    a = se.norm_url("https://www.example.org/page/?utm_source=x&id=2#top")
    b = se.norm_url("http://example.org/page?id=2")
    assert a == b


def test_fuse_puts_a_page_many_engines_agree_on_first() -> None:
    fused = se.fuse({"a": [_row("https://x.org/1"), _row("https://y.org/2")],
                     "b": [_row("https://y.org/2", snippet="по-дълго описание"), _row("https://z.org/3")]},
                    {"a": 1.0, "b": 1.0})
    assert fused[0]["url"] == "https://y.org/2"
    assert fused[0]["engines"] == ["a", "b"]
    assert fused[0]["snippet"] == "по-дълго описание"


def _engines(monkeypatch, api=(), free=(), browser=()):
    monkeypatch.setattr(se, "API_ENGINES", list(api))
    monkeypatch.setattr(se, "FREE_ENGINES", list(free))
    monkeypatch.setattr(se, "BROWSER_ENGINES", list(browser))


def test_a_blocked_engine_rests_and_is_not_asked_again(monkeypatch) -> None:
    calls: list[str] = []

    def captcha(q, n, region):
        calls.append(q)
        raise se.Blocked("CAPTCHA", 900)
    _engines(monkeypatch, free=[("ddg", captcha, 1.0), ("ok", lambda q, n, r: [_row("https://a.org")], 1.0)])
    assert se.multi_search("q1", browser=False)[0]["url"] == "https://a.org"
    se.multi_search("q2", browser=False)
    assert calls == ["q1"]
    assert se.cooling("ddg")


def test_api_engine_needs_its_key(monkeypatch) -> None:
    used: list[str] = []

    def api(q, n, r):
        used.append(q)
        return [_row("https://api.org")]
    _engines(monkeypatch, api=[("tav", api, "TAV_KEY", 1.2)])
    assert se.multi_search("q", browser=False) == []
    monkeypatch.setattr(se, "_key", lambda name: "secret" if name == "TAV_KEY" else "")
    assert se.multi_search("q", browser=False)[0]["url"] == "https://api.org"
    assert used == ["q"]


def test_browser_only_when_web_results_are_few(monkeypatch) -> None:
    browsed: list[str] = []

    def browser_engine(q, n, r):
        browsed.append(q)
        return [_row("https://b.org/1"), _row("https://b.org/2"), _row("https://b.org/3")]
    monkeypatch.setattr("genesis_agent.search_browser.available", lambda: True)
    many = [_row(f"https://w.org/{i}") for i in range(3)]
    _engines(monkeypatch, free=[("web", lambda q, n, r: many, 1.0)],
             browser=[("ddg_browser", browser_engine, 1.0), ("brave_browser", browser_engine, 1.0)])
    se.multi_search("enough")
    assert browsed == []
    _engines(monkeypatch, free=[("wikipedia", lambda q, n, r: many, 0.8)],
             browser=[("ddg_browser", browser_engine, 1.0), ("brave_browser", browser_engine, 1.0)])
    out = se.multi_search("only wiki")
    assert browsed == ["only wiki"]          # Brave не се пита — DuckDuckGo стигна
    assert any(r["url"].startswith("https://b.org") for r in out)


def test_a_slow_engine_does_not_hold_the_search(monkeypatch) -> None:
    import threading
    gate = threading.Event()

    def slow(q, n, r):
        gate.wait(5)
        return [_row("https://slow.org")]
    _engines(monkeypatch, free=[("slow", slow, 1.0), ("fast", lambda q, n, r: [_row("https://fast.org")], 1.0)])
    try:
        out = se.multi_search("q", timeout=0.3, browser=False)
    finally:
        gate.set()
    assert [r["url"] for r in out] == ["https://fast.org"]


@pytest.mark.parametrize(("code", "rest"), [(429, 300.0), (402, 6 * 3600.0), (401, 3600.0)])
def test_http_refusals_become_blocked(monkeypatch, code, rest) -> None:
    import urllib.error

    def refuse(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, code, "no", {}, None)  # type: ignore[arg-type]
    monkeypatch.setattr(se.urllib.request, "urlopen", refuse)
    with pytest.raises(se.Blocked) as e:
        se._http("https://api.example/search")
    assert e.value.seconds == rest


DDG_HTML = """
<div class="result"><a rel="nofollow" class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fbg.wikipedia.org%2Fwiki%2F%D0%9C%D1%83%D1%81%D0%B0%D0%BB%D0%B0&amp;rut=x">Мусала - Уикипедия</a>
<a class="result__snippet" href="#">Мусала е най-високият връх, <b>2925</b> m.</a></div>
"""


def test_ddg_html_is_parsed_and_redirects_unwrapped(monkeypatch) -> None:
    monkeypatch.setattr("genesis_agent.web_search._http_post", lambda url, form, timeout=10: DDG_HTML)
    rows = se._ddg_html("Мусала", 5, "bg-bg")
    assert rows == [{"title": "Мусала - Уикипедия", "url": "https://bg.wikipedia.org/wiki/Мусала",
                     "snippet": "Мусала е най-високият връх, 2925 m."}]


def test_ddg_html_captcha_is_blocked(monkeypatch) -> None:
    page = "<div class='anomaly-modal'>Unfortunately, bots use DuckDuckGo too.</div>"
    monkeypatch.setattr("genesis_agent.web_search._http_post", lambda url, form, timeout=10: page)
    with pytest.raises(se.Blocked):
        se._ddg_html("Мусала", 5, "bg-bg")


def test_tavily_rows(monkeypatch) -> None:
    monkeypatch.setattr(se, "_http", lambda url, **kw: json.dumps({"results": [
        {"title": "A", "url": "https://a.org", "content": "  много \n текст  "}, {"title": "без адрес"}]}))
    assert se._tavily("q", 5, "bg-bg") == [{"title": "A", "url": "https://a.org", "snippet": "много текст"}]


def test_serper_answer_box_comes_first(monkeypatch) -> None:
    monkeypatch.setattr(se, "_http", lambda url, **kw: json.dumps({
        "answerBox": {"title": "Musala", "answer": "2,925 m", "link": "https://box.org"},
        "organic": [{"title": "O", "link": "https://o.org", "snippet": "s"}]}))
    rows = se._serper("q", 5, "bg-bg")
    assert [r["url"] for r in rows] == ["https://box.org", "https://o.org"]
    assert rows[0]["snippet"] == "2,925 m"


def test_browser_channels_fall_back_to_system_edge(monkeypatch) -> None:
    from genesis_agent import search_browser as sb
    monkeypatch.delenv("GENESIS_BROWSER_CHANNEL", raising=False)
    assert sb._channels() == ["", "msedge", "chrome"]
    monkeypatch.setenv("GENESIS_BROWSER_CHANNEL", "chrome")
    assert sb._channels() == ["chrome", "", "msedge"]


def _resolve(monkeypatch, table: dict[str, str]) -> None:
    import socket

    def fake(host, *a, **kw):
        if host not in table:
            raise socket.gaierror(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 0))]
    monkeypatch.setattr("genesis_agent.web_fetch.socket.getaddrinfo", fake)


def test_search_browser_never_allows_loopback_even_after_browse_did(monkeypatch) -> None:
    """Преглед за сигурност 2026-11-17: правилото беше общо с BROWSE и след
    BROWSE към localhost страниците от търсенето също можеха към localhost."""
    from genesis_agent import browser
    from genesis_agent import search_browser as sb
    monkeypatch.setattr(browser, "_allow_loopback", True)
    assert sb._refusal("http://localhost:2375/containers/json")
    assert sb._refusal("http://127.0.0.1:11434/api/tags")
    assert sb._refusal("http://169.254.169.254/latest/meta-data/")
    assert sb._refusal("https://93.184.216.34/") == ""
    assert sb._refusal("data:text/html,x") == ""


def test_refusal_fails_closed(monkeypatch) -> None:
    """Втори преглед 2026-11-17: `x.localhost` Python не го разрешава (gaierror
    → беше „може“), а Chromium го праща към 127.0.0.1."""
    from genesis_agent import search_browser as sb
    _resolve(monkeypatch, {"rebind.example.com": "192.168.0.1"})
    assert sb._refusal("http://evil.localhost:2375/")
    assert sb._refusal("https://no-such-name.example/")
    assert sb._refusal("https://rebind.example.com/admin")


class _Route:
    def __init__(self, url: str, kind: str = "fetch", method: str = "GET") -> None:
        self.request = type("Req", (), {"url": url, "resource_type": kind, "method": method})()
        self.done = ""
        self.body = ""

    async def abort(self, why: str = "") -> None:
        self.done = "abort"

    async def continue_(self) -> None:
        self.done = "continue"

    async def fulfill(self, status: int = 200, content_type: str = "", body: str = "") -> None:
        self.done, self.body = "fulfill", body


def _drive(handler, route: _Route) -> str:
    import asyncio
    asyncio.run(handler(route))
    return route.done


def test_a_foreign_page_never_lets_chromium_connect(monkeypatch) -> None:
    """DNS rebinding: проверка в Python, после връзка от Chromium = два отговора
    на DNS. Затова Chromium не се свързва: всяка заявка се тегли от
    web_fetch.fetch (проверка на сокета) и се връща с fulfill."""
    from genesis_agent import search_browser as sb
    _resolve(monkeypatch, {"news.example.com": "93.184.216.34", "rebind.example.com": "192.168.0.1"})
    fetched: list[str] = []

    def fake_fetch(url):
        fetched.append(url)
        return url, "text/html; charset=windows-1251", "<p>текст</p>"
    monkeypatch.setattr("genesis_agent.web_fetch.fetch", fake_fetch)
    ok = _Route("https://news.example.com/a", kind="document")
    assert _drive(sb._guarded_route, ok) == "fulfill" and ok.body == "<p>текст</p>"
    assert _drive(sb._guarded_route, _Route("https://rebind.example.com/admin")) == "abort"
    assert _drive(sb._guarded_route, _Route("http://evil.localhost/")) == "abort"
    assert _drive(sb._guarded_route, _Route("https://news.example.com/api", method="POST")) == "abort"
    assert fetched == ["https://news.example.com/a"]


def test_a_search_engine_page_only_talks_to_its_own_domain() -> None:
    from genesis_agent import search_browser as sb
    handler = sb._engine_route("duckduckgo.com")
    assert _drive(handler, _Route("https://links.duckduckgo.com/d.js")) == "continue"
    assert _drive(handler, _Route("https://duckduckgo.com.evil.example/x")) == "abort"
    assert _drive(handler, _Route("http://192.168.0.1/")) == "abort"
    assert _drive(handler, _Route("https://duckduckgo.com/logo.png", kind="image")) == "abort"


def test_research_does_not_open_localhost_from_search_results(monkeypatch) -> None:
    """fetch() пуска localhost, когато адресът сам е localhost — а тук адресът
    идва от чужд резултат в търсачката."""
    from genesis_agent import research as rs

    def no_fetch(url):
        raise AssertionError(f"не биваше да се отваря: {url}")
    monkeypatch.setattr("genesis_agent.web_fetch.fetch", no_fetch)
    assert rs._fetch_text("http://localhost:8787/demo") == ("", "")
    assert rs._fetch_text("http://[::1]:11434/") == ("", "")


def test_the_whole_context_is_locked_and_fails_closed() -> None:
    """Трети преглед 2026-11-17: защитата беше на нашите страници — изскачащ
    прозорец от чужда страница беше без нея; а без route_web_socket (стар
    Playwright) WebSocket тихо оставаше отворен."""
    import asyncio

    import pytest

    from genesis_agent import search_browser as sb

    class _Ctx:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def route(self, pattern, handler):
            self.calls.append(f"route:{handler.__name__}")

        async def route_web_socket(self, pattern, handler):
            self.calls.append("ws")

        async def add_init_script(self, script):
            self.calls.append("init" if "RTCPeerConnection" in script and "WebSocket" in script else "?")
    ctx = _Ctx()
    asyncio.run(sb._lock_context(ctx))
    assert ctx.calls == ["route:_deny_route", "ws", "init"]

    class _Old:
        async def route(self, pattern, handler):
            return None

        async def add_init_script(self, script):
            return None
    with pytest.raises(AttributeError):
        asyncio.run(sb._lock_context(_Old()))
    route = _Route("https://example.org/")
    assert _drive(sb._deny_route, route) == "abort"
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in sb._LAUNCH_ARGS


def test_the_dead_end_proxy_closes_every_connection() -> None:
    """Четвърти преглед 2026-11-17: workers/QUIC/празен iframe минават покрай
    init script и route(). Контекстът за чужди страници има прокси, което
    затваря всяка връзка — каквото Chromium изпрати сам, не стига до никъде."""
    import socket
    import urllib.parse

    from genesis_agent import search_browser as sb
    url = sb._dead_end_proxy()
    assert url == sb._dead_end_proxy()   # едно за процеса
    parts = urllib.parse.urlsplit(url)
    with socket.create_connection((parts.hostname, parts.port), timeout=5) as s:
        s.settimeout(5)
        try:
            s.sendall(b"CONNECT 192.168.0.1:80 HTTP/1.1\r\nHost: 192.168.0.1:80\r\n\r\n")
            got = s.recv(100)
        except ConnectionError:   # Windows: RST (10054) или прекъсната (10053) — CI 2026-11-17
            got = b""
        assert got == b""   # затворено, без отговор


def test_browser_is_off_in_tests() -> None:
    from genesis_agent import search_browser as sb
    assert not sb.available()
    assert sb.search("ddg_browser", "q") == []


# ── 2026-11-23: ключовете за търсене от ~/.genesis/.env ─────────────────────
# Проверка, не нова функция: TAVILY_API_KEY и SERPER_API_KEY вече са в
# API_ENGINES. Тук е целият път наведнъж — ключ само във файла (не в средата),
# подменен HTTP, двете търсачки в сливането. conftest подменя `_key` с празно
# за всеки тест, затова истинската функция е взета при внасянето (_REAL_KEY).
def _fake_search_http(seen: list[tuple[str, dict]]):
    def http(url, *, data=None, headers=None, **kw):
        seen.append((url, dict(headers or {})))
        if "tavily" in url:
            return json.dumps({"results": [
                {"title": "Общ", "url": "https://both.org/page", "content": "от tavily"},
                {"title": "T", "url": "https://only-tavily.org", "content": "t"}]})
        if "serper" in url:
            return json.dumps({"organic": [
                {"title": "S", "link": "https://only-serper.org", "snippet": "s"},
                {"title": "Общ", "link": "https://www.both.org/page/", "snippet": "от serper, по-дълго"}]})
        raise AssertionError(f"неочаквана заявка: {url}")
    return http


def _only_the_env_file(monkeypatch, tmp_path, text: str) -> None:
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    monkeypatch.setattr("genesis_agent.paths.ENV_FILES", (str(env),))
    for name in ("TAVILY_API_KEY", "SERPER_API_KEY", "BRAVE_API_KEY", "JINA_API_KEY", "SEARXNG_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(se, "_key", _REAL_KEY)
    monkeypatch.setattr(se, "FREE_ENGINES", [])


def test_tavily_and_serper_keys_from_the_env_file_join_the_fusion(monkeypatch, tmp_path) -> None:
    _only_the_env_file(monkeypatch, tmp_path,
                       "TAVILY_API_KEY=tvly-test\nexport SERPER_API_KEY=serper-test\n")
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(se, "_http", _fake_search_http(seen))

    assert se.configured_api_engines() == ["tavily", "serper"]
    rows = se.multi_search("връх Мусала", browser=False)

    # Страницата, която и двете дават, е първа и носи имената им.
    assert se.norm_url(rows[0]["url"]) == se.norm_url("https://both.org/page")
    assert sorted(rows[0]["engines"]) == ["serper", "tavily"]
    assert len(rows) == 3
    sent = {("tavily" if "tavily" in u else "serper"): h for u, h in seen}
    assert sent["tavily"]["Authorization"] == "Bearer tvly-test"
    assert sent["serper"]["X-API-KEY"] == "serper-test"


def test_without_the_keys_no_request_goes_to_tavily_or_serper(monkeypatch, tmp_path) -> None:
    _only_the_env_file(monkeypatch, tmp_path, "TAVILY_API_KEY=\n")   # празно не е ключ
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(se, "_http", _fake_search_http(seen))

    assert se.configured_api_engines() == []
    assert se.multi_search("q", browser=False) == []
    assert seen == []
