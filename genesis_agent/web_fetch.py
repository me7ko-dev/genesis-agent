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
from html.parser import HTMLParser
from typing import ClassVar
from urllib.parse import urljoin, urlparse

_TIMEOUT = 20
_MAX_BYTES = 3_000_000
_MAX_REDIRECTS = 5
_MAX_CHARS = 30_000
_UA = "Mozilla/5.0 (compatible; Genesis-Agent WEB_FETCH)"


class FetchError(Exception):
    pass


def _check_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError(f"само http(s) адреси: {url[:200]}")
    try:
        literal = ipaddress.ip_address(parsed.hostname.strip("[]"))
        addresses = [literal]
    except ValueError:
        try:
            infos = socket.getaddrinfo(parsed.hostname,
                                       parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as e:
            # Зад прокси (HTTPS_PROXY) името разрешава проксито, не тази машина.
            import requests
            if requests.utils.get_environ_proxies(url):
                return
            raise FetchError(f"няма такъв адрес ({parsed.hostname}): {e}") from e
        addresses = [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]
    for ip in addresses:
        if ip.is_loopback:
            continue
        if ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise FetchError(f"{parsed.hostname} е във вътрешна мрежа ({ip}) — не се отваря оттук")


class _Markdown(HTMLParser):
    SKIP: ClassVar[set[str]] = {"script", "style", "noscript", "nav", "footer", "aside", "svg",
                                "form", "button", "iframe", "template"}
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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
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
        elif tag in self.BLOCK or tag == "td":
            self.out.append("\n" if tag != "td" else " | ")

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
            text = " ".join("".join(self.link_text).split())
            self.out.append(f"[{text}]({self.href})" if text else "")
            self.href = None
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


def fetch(url: str) -> tuple[str, str, str]:
    """(final url, content type, body text) — redirects followed by hand, each
    hop checked against internal networks."""
    import requests
    url = url.strip()
    if url.startswith("www."):
        url = "https://" + url
    for _ in range(_MAX_REDIRECTS + 1):
        _check_host(url)
        try:
            resp = requests.get(url, headers={"User-Agent": _UA, "Accept": "text/html,text/plain,*/*"},
                                timeout=_TIMEOUT, allow_redirects=False, stream=True)
        except requests.RequestException as e:
            raise FetchError(f"не се отвори: {e}") from e
        if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
            target = resp.headers.get("location") or ""
            resp.close()
            if not target:
                raise FetchError(f"пренасочване без адрес ({resp.status_code})")
            url = urljoin(url, target)
            continue
        if resp.status_code >= 400:
            resp.close()
            raise FetchError(f"HTTP {resp.status_code}")
        data = b""
        for chunk in resp.iter_content(65536):
            data += chunk
            if len(data) > _MAX_BYTES:
                break
        resp.close()
        ctype = resp.headers.get("content-type", "")
        encoding = resp.encoding or "utf-8"
        textual = ("html" in ctype or "charset" in ctype
                   or ctype.startswith(("text/", "application/json", "application/xml")))
        if not textual:
            raise FetchError(f"не е текст ({ctype or 'неизвестен тип'}) — ползвай RUN_CMD curl -o")
        return url, ctype, data[:_MAX_BYTES].decode(encoding, errors="replace")
    raise FetchError(f"над {_MAX_REDIRECTS} пренасочвания")


def web_fetch(url: str) -> str:
    """The tool: the page as text, or why not. Never raises."""
    try:
        final, ctype, body = fetch(url)
    except FetchError as e:
        return f"[WEB_FETCH: {url[:200]}] ❌ {e}"
    if "html" in ctype or body.lstrip()[:200].lower().startswith(("<!doctype html", "<html")):
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
