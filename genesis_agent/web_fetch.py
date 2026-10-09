"""
genesis_agent.web_fetch — WEB_FETCH: a page by its address, as readable text
(Claude Code's WebFetch). The documentation the operator linked, a changelog,
an API reference — without starting a browser (BROWSE needs Playwright) and
without the search results WEB_SEARCH gives instead of the page itself.

The text keeps what a reader needs: headings (`#`), list items, links as
`[text](url)`, code blocks; scripts, styles, navigation and footers go.
Only http(s); at most 5 redirects, 3 MB, 20 s. Addresses in private and
link-local networks are refused — including after a redirect: a page must
not be able to send the agent to the router or to the cloud metadata
service (169.254.169.254) and hand what it finds to the model. Loopback
(localhost) is allowed: checking your own dev server is everyday work.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import urljoin, urlparse

_TIMEOUT = 20          # за ЦЯЛОТО четене, не за всяко парче (одит 2026-10-09: бавен
_MAX_BYTES = 3_000_000  # сървър с байт на 0.4 s държеше инструмента с часове)
_MAX_REDIRECTS = 5
_MAX_CHARS = 30_000
_UA = "Mozilla/5.0 (compatible; Genesis-Agent WEB_FETCH)"


class FetchError(Exception):
    pass


# Проверката е на самата връзка (одит 2026-10-09): проверка на името отделно
# от свързването се заобикаляше — `http://10.0.0.1\@example.com/` urlparse
# вижда като example.com, а urllib3 се свързва с 10.0.0.1; DNS, който отговаря
# различно при второто питане, също. Сега адресът, с който се свързваме, е
# този, който е проверен.
_guard = threading.local()


def _allowed_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_loopback: bool) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return allow_loopback
    # is_global, не списък от изключения: 100.64/10 (CGNAT, Tailscale, metadata
    # на Alibaba 100.100.100.200) не е нито private, нито reserved.
    return ip.is_global


def _connect(sock: socket.socket, sockaddr: Any) -> None:
    """Отделено, за да могат тестовете да насочат проверения адрес към локален сървър."""
    sock.connect(sockaddr)


def _guarded_connection(address: tuple[str, int], timeout: Any, source_address: Any,
                        socket_options: Any) -> socket.socket:
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    refused = ""
    last_error: OSError | None = None
    for family, kind, proto, _, sockaddr in infos:
        ip = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        if not _allowed_ip(ip, getattr(_guard, "allow_loopback", False)):
            refused = f"{host} е във вътрешна мрежа ({ip}) — не се отваря оттук"
            continue
        # Свързване с проверения sockaddr, без ново разрешаване на името.
        sock = socket.socket(family, kind, proto)
        try:
            for opt in socket_options or []:
                sock.setsockopt(*opt)
            if isinstance(timeout, (int, float)):
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            _connect(sock, sockaddr)
            return sock
        except OSError as e:
            last_error = e
            sock.close()
    if last_error is not None and not refused:
        raise last_error
    _guard.refused = refused or f"{host}: няма адрес"
    raise OSError(_guard.refused)


def _session():
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
    from urllib3.exceptions import NameResolutionError, NewConnectionError

    class _Guard:
        def _new_conn(self):  # type: ignore[no-untyped-def]
            try:
                return _guarded_connection((self._dns_host, self.port), self.timeout,
                                           self.source_address, self.socket_options)
            except socket.gaierror as e:
                raise NameResolutionError(self.host, self, e) from e
            except OSError as e:
                raise NewConnectionError(self, str(e)) from e

    class _HTTP(_Guard, HTTPConnection):
        pass

    class _HTTPS(_Guard, HTTPSConnection):
        pass

    class _HTTPPool(HTTPConnectionPool):
        ConnectionCls = _HTTP

    class _HTTPSPool(HTTPSConnectionPool):
        ConnectionCls = _HTTPS

    class _Adapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {"http": _HTTPPool, "https": _HTTPSPool}

    session = requests.Session()
    session.mount("http://", _Adapter())
    session.mount("https://", _Adapter())
    return session


def _is_loopback_name(host: str) -> bool:
    host = host.strip("[]").rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_url(url: str) -> str:
    """The host to connect to, after refusing URLs whose host is ambiguous."""
    from urllib3.util import parse_url
    try:
        parsed = urlparse(url)
        parsed.port  # noqa: B018 — хвърля ValueError при невалиден порт
    except ValueError as e:
        raise FetchError(f"невалиден адрес: {e}") from e
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError(f"само http(s) адреси: {url[:200]}")
    if "\\" in url or "@" in parsed.netloc:
        raise FetchError("адрес с потребител или „\\“ в него не се отваря (двусмислен хост)")
    try:
        other = (parse_url(url).host or "").strip("[]").lower()
        port = parsed.port
    except ValueError as e:
        raise FetchError(f"невалиден адрес: {e}") from e
    host = parsed.hostname.strip("[]").lower()
    if other != host:
        raise FetchError(f"двусмислен хост ({host} / {other}) — не се отваря")
    if port is not None and not 0 < port < 65536:
        raise FetchError("невалиден порт")
    return host


def _proxied(url: str) -> bool:
    import requests
    return bool(requests.utils.select_proxy(url, requests.utils.get_environ_proxies(url)))


def _precheck_through_proxy(host: str, url: str, allow_loopback: bool) -> None:
    """Зад прокси свързването е с проксито — проверяваме името сами, доколкото
    се разрешава оттук; неразрешимо име разрешава проксито (външен адрес)."""
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)
        except socket.gaierror:
            return
        addresses = [ipaddress.ip_address(str(i[4][0]).split("%", 1)[0]) for i in infos]
    for ip in addresses:
        if not _allowed_ip(ip, allow_loopback):
            raise FetchError(f"{host} е във вътрешна мрежа ({ip}) — не се отваря оттук")


class _Markdown(HTMLParser):
    # Без "form": страници (ASP.NET WebForms) слагат ЦЯЛОТО тяло във форма.
    SKIP: ClassVar[set[str]] = {"script", "style", "noscript", "nav", "footer", "aside", "svg",
                                "iframe", "template"}
    BLOCK: ClassVar[set[str]] = {"p", "div", "section", "article", "main", "header", "table",
                                 "tr", "br", "hr", "blockquote", "ul", "ol", "dl", "figure"}

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.href: str | None = None
        self.link_text: list[str] = []
        self.title = ""
        self._in_title = False

    def _flush_link(self) -> None:
        """Незатворено <a> не поглъща останалата страница."""
        if self.href is not None:
            text = " ".join("".join(self.link_text).split())
            self.out.append(f"[{text}]({self.href})" if text else "")
            self.href = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "a" or tag in self.BLOCK or tag in ("li", "pre", "td", "th") \
                or re.fullmatch(r"h[1-6]", tag):
            self._flush_link()
        if tag == "title":
            self._in_title = True
        elif re.fullmatch(r"h[1-6]", tag):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag == "pre":
            self.pre += 1
            self.out.append("\n```\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("javascript:", "#")):
                self.href = urljoin(self.base, href)
                self.link_text = []
        elif tag in self.BLOCK or tag in ("td", "th"):
            self.out.append(" | " if tag in ("td", "th") else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag == "a" and self.href is not None:
            self._flush_link()
        elif re.fullmatch(r"h[1-6]", tag) or tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self.skip:
            return
        if self.href is not None:
            self.link_text.append(data)
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))

    def text(self) -> str:
        self._flush_link()
        raw = "".join(self.out)
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def html_to_text(html: str, base: str = "") -> tuple[str, str]:
    """(title, readable text) of an HTML page."""
    parser = _Markdown(base)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return "", re.sub(r"<[^>]+>", " ", html)
    return " ".join(parser.title.split()), parser.text()


_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9_-]+)""", re.IGNORECASE)


def _decode(data: bytes, ctype: str) -> str:
    """Charset от заглавката, иначе от <meta charset>, иначе UTF-8 — не
    ISO-8859-1, с което requests четеше кирилицата на всяка страница без
    charset в заглавката (одит 2026-10-09)."""
    m = re.search(r"charset=([A-Za-z0-9_-]+)", ctype, re.IGNORECASE)
    charset = m.group(1) if m else ""
    if not charset:
        meta = _META_CHARSET.search(data[:4096])
        charset = meta.group(1).decode("ascii", "replace") if meta else "utf-8"
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def fetch(url: str) -> tuple[str, str, str]:
    """(final url, content type, body text). Redirects followed by hand; every
    connection checked at the socket. Raises FetchError only."""
    import requests
    url = url.strip()
    if url.startswith("www."):
        url = "https://" + url
    first_host = _check_url(url)
    # localhost само ако операторът/моделът сам го е поискал — страница навън
    # не може да пренасочи агента към kubectl proxy, Docker API и т.н.
    allow_loopback = _is_loopback_name(first_host)
    deadline = time.monotonic() + _TIMEOUT
    session = _session()
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            host = _check_url(url)
            proxied = _proxied(url)
            if proxied:
                _precheck_through_proxy(host, url, allow_loopback)
            _guard.allow_loopback = allow_loopback
            _guard.refused = ""
            left = deadline - time.monotonic()
            if left <= 0:
                raise FetchError(f"над {_TIMEOUT} s")
            try:
                resp = session.get(url, headers={"User-Agent": _UA,
                                                 "Accept": "text/html,text/plain,*/*"},
                                   timeout=(min(10.0, left), left), allow_redirects=False,
                                   stream=True)
            except requests.RequestException as e:
                raise FetchError(getattr(_guard, "refused", "") or f"не се отвори: {e}") from e
            try:
                if resp.status_code in (301, 302, 303, 307, 308):
                    target = resp.headers.get("location") or ""
                    if not target:
                        raise FetchError(f"пренасочване без адрес ({resp.status_code})")
                    url = urljoin(url, target)
                    continue
                if resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "")
                low = ctype.lower()
                textual = ("html" in low or "charset" in low
                           or low.startswith(("text/", "application/json", "application/xml")))
                if not textual:
                    raise FetchError(f"не е текст ({ctype or 'неизвестен тип'}) — ползвай RUN_CMD curl -o")
                data = b""
                for chunk in resp.iter_content(65536):
                    data += chunk
                    if len(data) > _MAX_BYTES:
                        break
                    if time.monotonic() > deadline:
                        raise FetchError(f"над {_TIMEOUT} s")
                return url, ctype, _decode(data[:_MAX_BYTES], ctype)
            except requests.RequestException as e:
                raise FetchError(f"прекъсна: {e}") from e
            finally:
                resp.close()
        raise FetchError(f"над {_MAX_REDIRECTS} пренасочвания")
    except FetchError:
        raise
    except (ValueError, LookupError, UnicodeError, OSError) as e:
        raise FetchError(str(e)) from e
    finally:
        session.close()
        _guard.allow_loopback = False


def web_fetch(url: str) -> str:
    """The tool: the page as text, or why not. Never raises."""
    try:
        final, ctype, body = fetch(url)
    except FetchError as e:
        return f"[WEB_FETCH: {url[:200]}] ❌ {e}"
    if "html" in ctype.lower() or body.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
        title, text = html_to_text(body, final)
    else:
        title, text = "", body
    note = ""
    if len(text) > _MAX_CHARS:
        note = f"\n… [отрязано: показани {_MAX_CHARS} от {len(text)} знака]"
        text = text[:_MAX_CHARS]
    moved = f" (→ {final})" if final.rstrip("/") != url.strip().rstrip("/") else ""
    head = f"# {title}\n\n" if title else ""
    return f"[WEB_FETCH: {url[:200]}]{moved}\n{head}{text}{note}"
