"""genesis_agent.search_engines — едно търсене, много търсачки, паралелно.

Защо (мерено на лаптопа 2026-11-17, scripts/bench_research.py): с единствената
търсачка DuckDuckGo Lite RESEARCH отговаряше вярно на 2 от 20 въпроса —
DuckDuckGo дава CAPTCHA на кирилица, а след нея оставаше само Wikipedia.
Изстъргването на търсачки без браузър е загубена кауза (Bing връща чужди
резултати, Brave — 429, Google — празно), а през браузър работи, докато не
ни познаят (виж search_browser).

Затова стъпки, всяка по-бавна и по-крехка от предишната:
  1. API търсачки с ключ — сигурни и бързи (~0.5 s): TAVILY_API_KEY,
     SERPER_API_KEY, BRAVE_API_KEY, JINA_API_KEY, SEARXNG_URL (свой SearXNG).
     Ключ няма ли — стъпката я няма.
  2. Wikipedia API (без ключ, винаги отговаря) и DuckDuckGo html (без ключ,
     докато не почне CAPTCHA) — паралелно със стъпка 1.
  3. Ако уеб резултатите са под 3 — DuckDuckGo в браузъра, после Brave в
     браузъра.
Търсачка, която откаже (CAPTCHA, 429, 402, свършени кредити), почива
(cool_down) — повтарянето само удължава блокирането.

Резултатите се сливат по адрес (reciprocal rank fusion): страница, която
няколко търсачки дават високо, излиза първа.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

log = logging.getLogger("genesis.search_engines")

ENGINE_TIMEOUT = 8.0
_UA_API = "genesis-agent"
_UA_WEB = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_cool: dict[str, float] = {}
_cool_lock = threading.Lock()


class Blocked(Exception):
    """Търсачката отказа (CAPTCHA/лимит/ключ) — да почива `seconds`."""

    def __init__(self, why: str, seconds: float = 600.0) -> None:
        super().__init__(why)
        self.seconds = seconds


def cooling(name: str) -> bool:
    with _cool_lock:
        return _cool.get(name, 0.0) > time.monotonic()


def cool_down(name: str, seconds: float) -> None:
    with _cool_lock:
        _cool[name] = time.monotonic() + seconds
    log.info("търсачката %s почива %.0f s", name, seconds)


def reset_cooldowns() -> None:
    with _cool_lock:
        _cool.clear()


def _key(name: str) -> str:
    """Ключ от средата или от .env на Genesis (средата печели)."""
    v = os.environ.get(name, "").strip()
    if v:
        return v
    try:
        from genesis_agent.paths import read_env_files
        return (read_env_files(name) or "").strip()
    except Exception:
        log.debug("ключът %s не се прочете", name, exc_info=True)
        return ""


def _blocked_for(code: int) -> float:
    """Колко да почива API търсачка според HTTP кода."""
    if code in (401, 403):
        return 3600.0      # грешен/спрян ключ — няма смисъл да пробваме скоро
    if code == 402:
        return 6 * 3600.0  # свършени безплатни кредити
    return 300.0           # 429 и останалите — лимит за минута


def _http(url: str, *, data: Any = None, headers: dict[str, str] | None = None,
          timeout: float = ENGINE_TIMEOUT, form: bool = False) -> str:
    h = {"User-Agent": _UA_API, "Accept": "application/json"}
    body: bytes | None = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 402, 403, 429):
            raise Blocked(f"HTTP {e.code}", _blocked_for(e.code)) from e
        raise


def _rows(items: Any, title: str, url: str, snippet: str) -> list[dict]:
    out = []
    for it in items or []:
        if isinstance(it, dict) and it.get(url):
            out.append({"title": str(it.get(title) or ""), "url": str(it[url]),
                        "snippet": " ".join(str(it.get(snippet) or "").split())[:600]})
    return out


# ─── API търсачки (с ключ) ───────────────────────────────────────────────────

def _tavily(q: str, n: int, region: str) -> list[dict]:
    d = json.loads(_http("https://api.tavily.com/search", data={
        "query": q, "max_results": n, "search_depth": "basic", "include_answer": False},
        headers={"Authorization": f"Bearer {_key('TAVILY_API_KEY')}"}))
    return _rows(d.get("results"), "title", "url", "content")


def _serper(q: str, n: int, region: str) -> list[dict]:
    gl = region.split("-")[0] if region else "bg"
    d = json.loads(_http("https://google.serper.dev/search", data={"q": q, "num": n, "gl": gl},
                         headers={"X-API-KEY": _key("SERPER_API_KEY")}))
    out: list[dict] = []
    box = d.get("answerBox") or {}
    if box.get("link") and (box.get("answer") or box.get("snippet")):
        out.append({"title": str(box.get("title") or "Отговор"), "url": str(box["link"]),
                    "snippet": str(box.get("answer") or box.get("snippet"))[:600]})
    return out + _rows(d.get("organic"), "title", "link", "snippet")


def _brave_api(q: str, n: int, region: str) -> list[dict]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({"q": q, "count": n})
    d = json.loads(_http(url, headers={"X-Subscription-Token": _key("BRAVE_API_KEY")}))
    return _rows((d.get("web") or {}).get("results"), "title", "url", "description")


def _jina(q: str, n: int, region: str) -> list[dict]:
    d = json.loads(_http("https://s.jina.ai/?" + urllib.parse.urlencode({"q": q}), headers={
        "Authorization": f"Bearer {_key('JINA_API_KEY')}", "X-Respond-With": "no-content"}))
    return _rows(d.get("data"), "title", "url", "description")[:n]


def _searxng(q: str, n: int, region: str) -> list[dict]:
    base = _key("SEARXNG_URL").rstrip("/")
    d = json.loads(_http(f"{base}/search?" + urllib.parse.urlencode({"q": q, "format": "json"})))
    return _rows(d.get("results"), "title", "url", "content")[:n]


# ─── Без ключ ────────────────────────────────────────────────────────────────

def _wikipedia(q: str, n: int, region: str) -> list[dict]:
    from genesis_agent.web_search import _wikipedia_search
    return _wikipedia_search(q, min(n, 3))


_DDG_LINK = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_DDG_SNIP = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)


def _ddg_html(q: str, n: int, region: str) -> list[dict]:
    from genesis_agent import web_search as ws
    html = ws._http_post("https://html.duckduckgo.com/html/", {"q": q, "kl": region}, timeout=8)
    if ws._is_challenge(html) or ("result__a" not in html and "anomaly" in html):
        raise Blocked("CAPTCHA", 900.0)
    out = []
    snippets = [ws._extract_text(s) for s in _DDG_SNIP.findall(html)]
    for i, (href, title) in enumerate(_DDG_LINK.findall(html)[:n]):
        url = href
        m = re.search(r"uddg=([^&]+)", url)
        if m:
            url = urllib.parse.unquote(m.group(1))
        elif url.startswith("//"):
            url = "https:" + url
        if "duckduckgo.com/y.js" in url:   # реклама
            continue
        out.append({"title": ws._extract_text(title).strip(), "url": url,
                    "snippet": snippets[i][:400] if i < len(snippets) else ""})
    return out


def _browser(engine: str) -> Callable[[str, int, str], list[dict]]:
    def run(q: str, n: int, region: str) -> list[dict]:
        from genesis_agent import search_browser as sb
        try:
            return sb.search(engine, q, n, region=region)
        except sb.Blocked as e:
            raise Blocked("CAPTCHA в браузъра", 1800.0) from e
    return run


Engine = Callable[[str, int, str], list[dict]]

# (име, функция, ключ/настройка, тегло при сливането)
API_ENGINES: list[tuple[str, Engine, str, float]] = [
    ("tavily", _tavily, "TAVILY_API_KEY", 1.2),
    ("serper", _serper, "SERPER_API_KEY", 1.2),
    ("brave_api", _brave_api, "BRAVE_API_KEY", 1.1),
    ("jina", _jina, "JINA_API_KEY", 1.0),
    ("searxng", _searxng, "SEARXNG_URL", 1.0),
]
FREE_ENGINES: list[tuple[str, Engine, float]] = [
    ("wikipedia", _wikipedia, 0.8),
    ("ddg_html", _ddg_html, 1.0),
]
BROWSER_ENGINES: list[tuple[str, Engine, float]] = [
    ("ddg_browser", _browser("ddg_browser"), 1.0),
    ("brave_browser", _browser("brave_browser"), 1.0),
]


def configured_api_engines() -> list[str]:
    return [name for name, _f, key, _w in API_ENGINES if _key(key)]


def norm_url(url: str) -> str:
    """Един адрес за сравнение: без http/https, www., #, проследяване и / накрая."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    query = "&".join(kv for kv in p.query.split("&")
                     if kv and not kv.lower().startswith(("utm_", "fbclid", "gclid", "ref=")))
    return f"{host}{p.path.rstrip('/')}{'?' + query if query else ''}"


def fuse(ranked: dict[str, list[dict]], weights: dict[str, float], k: int = 60) -> list[dict]:
    """Reciprocal rank fusion: сбор на тегло/(k + място) по търсачки."""
    merged: dict[str, dict] = {}
    for engine, rows in ranked.items():
        w = weights.get(engine, 1.0)
        for rank, r in enumerate(rows):
            key = norm_url(r["url"])
            cur = merged.get(key)
            if cur is None:
                cur = merged[key] = {"title": r.get("title", ""), "url": r["url"],
                                     "snippet": r.get("snippet", ""), "engines": [], "_score": 0.0}
            cur["_score"] += w / (k + rank)
            cur["engines"].append(engine)
            if len(r.get("snippet", "")) > len(cur["snippet"]):
                cur["snippet"] = r["snippet"]
            if not cur["title"]:
                cur["title"] = r.get("title", "")
    out = sorted(merged.values(), key=lambda r: -r["_score"])
    for r in out:
        r.pop("_score", None)
    return out


def _run(engines: list[tuple[str, Engine, float]], q: str, n: int, region: str,
         deadline: float) -> dict[str, list[dict]]:
    """Паралелно; закъснелите след deadline не се чакат."""
    if not engines:
        return {}
    got: dict[str, list[dict]] = {}
    pool = cf.ThreadPoolExecutor(max_workers=len(engines), thread_name_prefix="genesis-search")
    futs = {pool.submit(f, q, n, region): name for name, f, _w in engines}
    try:
        for fut in cf.as_completed(futs, timeout=max(0.1, deadline - time.monotonic())):
            name = futs[fut]
            try:
                rows = fut.result()
            except Blocked as e:
                cool_down(name, e.seconds)
                continue
            except Exception as e:  # noqa: BLE001 — една счупена търсачка не спира другите
                log.info("търсачката %s се провали: %s", name, e)
                continue
            if rows:
                got[name] = rows
    except cf.TimeoutError:
        log.info("търсачките %s не отговориха навреме",
                 ", ".join(futs[f] for f in futs if not f.done()))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return got


def multi_search(query: str, max_results: int = 8, *, region: str = "bg-bg",
                 timeout: float = 9.0, browser: bool = True) -> list[dict]:
    """Слетите резултати: [{title, url, snippet, engines}], най-добрите първи."""
    query = query.strip()
    if not query:
        return []
    t_end = time.monotonic() + timeout
    weights: dict[str, float] = {}
    tier1: list[tuple[str, Engine, float]] = []
    for name, f, key, w in API_ENGINES:
        if _key(key) and not cooling(name):
            tier1.append((name, f, w))
    tier1 += [(name, f, w) for name, f, w in FREE_ENGINES if not cooling(name)]
    for name, _f, w in tier1:
        weights[name] = w
    ranked = _run(tier1, query, max_results, region, t_end)

    def web_count() -> int:
        return len({norm_url(r["url"]) for e, rows in ranked.items() if e != "wikipedia" for r in rows})

    if browser and web_count() < 3:
        from genesis_agent import search_browser as sb
        if sb.available():
            for name, f, w in BROWSER_ENGINES:
                if web_count() >= 3 or time.monotonic() > t_end:
                    break
                if cooling(name):
                    continue
                weights[name] = w
                # Браузърът стартира ~1 s при първото търсене — даваме му поне 8 s.
                ranked.update(_run([(name, f, w)], query, max_results, region,
                                   max(t_end, time.monotonic() + 8.0)))
    return fuse(ranked, weights)[:max_results]
