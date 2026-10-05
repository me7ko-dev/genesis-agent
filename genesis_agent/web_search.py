#!/usr/bin/env python3
"""
genesis_agent/web_search.py — Browser/Web Interaction за Genesis Agent.

Поддържа:
  - DuckDuckGo (без API ключ — напълно безплатно); при CAPTCHA — Wikipedia API
  - Scraping на URL съдържание (urllib + html parser)
  - Кеш (избягва дублирани заявки)

Употреба:
    from genesis_agent.web_search import search, fetch_page

    results = search("python retry decorator best practices")
    for r in results:
        print(r['title'], r['url'])

    content = fetch_page("https://docs.python.org/3/")
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import ClassVar

from genesis_agent.config import DATA_DIR

log = logging.getLogger("genesis.web_search")

# ─── Кеш ─────────────────────────────────────────────────────────────────────

CACHE_DIR = DATA_DIR / ".search_cache"
CACHE_TTL_SECONDS = 3600  # 1 час


def _cache_key(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def _cache_get(key: str) -> str | None:
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - data["ts"] < CACHE_TTL_SECONDS:
            return data["content"]
    except (OSError, ValueError, LookupError, TypeError):
        pass
    return None


def _cache_set(key: str, content: str):
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    path.write_text(
        json.dumps({"ts": time.time(), "content": content}, ensure_ascii=False),
        encoding="utf-8"
    )


# ─── HTML Parser ─────────────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """Извлича чист текст от HTML."""
    SKIP_TAGS: ClassVar[set[str]] = {"script", "style", "head", "nav", "footer", "aside"}

    def __init__(self):
        super().__init__()
        self._skip = False
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.SKIP_TAGS:
            self._skip = True

    def handle_endtag(self, tag):
        if tag.lower() in self.SKIP_TAGS:
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            stripped = data.strip()
            if stripped:
                self._parts.append(stripped)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _extract_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        return parser.get_text()
    except Exception:
        log.debug("HTML парсерът падна — тагове с regex", exc_info=True)
        # Fallback — махаме HTML тагове с regex
        return re.sub(r"<[^>]+>", " ", html)


# ─── HTTP Helper ──────────────────────────────────────────────────────────────

def _http_get(url: str, timeout: int = 10) -> str:
    """Извлича HTML съдържание от URL."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Genesis/0.15 Python/3.11",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "bg,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            encoding = resp.headers.get_content_charset("utf-8")
            return raw.decode(encoding, errors="replace")
    except urllib.error.URLError as e:
        raise ConnectionError(f"Грешка при достъп до {url}: {e}")


# ─── DuckDuckGo Search ────────────────────────────────────────────────────────

def search(
    query: str,
    max_results: int = 5,
    *,
    use_cache: bool = True,
    region: str = "bg-bg",
) -> list[dict]:
    """
    Търси в DuckDuckGo и връща списък от резултати.

    Args:
        query:       Търсен текст.
        max_results: Максимален брой резултати.
        use_cache:   Дали да ползва кеш.
        region:      Регион ('bg-bg', 'en-us', etc.).

    Returns:
        list[dict] с ключове: title, url, snippet
    """
    cache_key = _cache_key(f"search:{query}:{max_results}:{region}")
    if use_cache:
        cached = _cache_get(cache_key)
        # Кеш отпреди поправката може да носи самата CAPTCHA страница като резултат.
        if cached and not _is_challenge(cached):
            return json.loads(cached)

    # Координати/адрес: геокодерът на OpenStreetMap, през urllib (правилно кодиран
    # UTF-8). `curl` с кирилица под Windows праща адреса в cp1251 и Nominatim
    # връща страница за грешка — 2026-09-28 моделът после налучка 41.52, 24.70.
    geo = _geocode(query) if _PLACE_QUERY.search(query) else []

    # DuckDuckGo Lite (не изисква JS)
    params = urllib.parse.urlencode({"q": query, "kl": region, "kp": "-1"})
    url = f"https://lite.duckduckgo.com/lite/?{params}"

    try:
        html = _http_get(url, timeout=8)
    except ConnectionError as e:
        log.error(f"[web_search] Търсачката недостъпна: {e}")
        html = ""
    if not html or _is_challenge(html):
        # 2026-09-28: DuckDuckGo (lite и html) и Mojeek връщат само CAPTCHA на
        # лаптопа. Страницата с „патиците“ се връщаше като резултат И се кешираше —
        # моделът търси координати три пъти подред и накрая ги измисля. Wikipedia
        # API отговаря без ключ, а за места дава и координатите.
        results = (geo + _wikipedia_search(query, max_results))[:max_results]
        if use_cache and results:
            _cache_set(cache_key, json.dumps(results, ensure_ascii=False))
        return results or [{"title": f"Търсачката отказа: {query}", "url": url,
                            "snippet": "DuckDuckGo върна CAPTCHA/не отговори, Wikipedia — нищо. "
                                       "Не измисляй фактите: вземи ги от конкретен източник "
                                       "(RUN_CMD curl към API/страница) или кажи, че не са проверени."}]

    results = []
    # Парсираме DuckDuckGo Lite — опитваме няколко HTML pattern-а
    patterns = [
        re.compile(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL),
        re.compile(r'<a[^>]+href="(//duckduckgo\.com/l/[^"]+)"[^>]*>(.*?)</a>', re.DOTALL),
        re.compile(r'<a[^>]+href="(https?://(?!duckduckgo)[^"]{10,})"[^>]*>(.*?)</a>', re.DOTALL),
    ]
    # DuckDuckGo Lite ползва еднични кавички за class= (но двойни за href=) —
    # regex-ът трябва да поддържа и двата стила, иначе snippet-ите тихо излизат
    # празни (открито при реален тест — заявката минаваше, но без съдържание).
    snippets_raw = re.findall(r"""class=['"]result-snippet['"][^>]*>(.*?)</td>""", html, re.DOTALL)
    snippets = [_extract_text(s) for s in snippets_raw]

    links: list[tuple[str, str]] = []
    for pat in patterns:
        links = pat.findall(html)
        if links:
            break

    for i, (url_raw, title_raw) in enumerate(links[:max_results]):
        title = _extract_text(title_raw).strip()
        if not title or len(title) < 3:
            continue
        snippet = snippets[i] if i < len(snippets) else ""
        result_url = url_raw
        if result_url.startswith("//"):
            result_url = "https:" + result_url
        if "uddg=" in result_url:
            m_uddg = re.search(r"uddg=([^&]+)", result_url)
            if m_uddg:
                # unquote върху низ не хвърля (невалидните байтове стават „�“).
                result_url = urllib.parse.unquote(m_uddg.group(1))
        results.append({"title": title, "url": result_url, "snippet": snippet[:300]})

    # Fallback: показваме извлечен текст ако не е парснат нищо
    if not results:
        log.debug("[web_search] Fallback: не са намерени структурирани резултати.")
        text = _extract_text(html)
        results = [{"title": f"Резултат за: {query}", "url": url, "snippet": text[:500]}]

    results = (geo + results)[:max_results]
    if use_cache and results:
        _cache_set(cache_key, json.dumps(results, ensure_ascii=False))

    return results


_CHALLENGE_MARKERS = ("bots use duckduckgo", "anomaly-modal", "unusual traffic", "captcha",
                      "are you a robot", "challenge-form")


def _is_challenge(html: str) -> bool:
    """Страница „докажи, че си човек“ вместо резултати."""
    low = html[:20000].lower()
    return any(m in low for m in _CHALLENGE_MARKERS)


_WIKI_NOISE = re.compile(r"\b(координати|координатите|gps|coordinates|latitude|longitude|lat|lon|"
                         r"географски|ширина|дължина|wikipedia|уикипедия)\b", re.IGNORECASE)


_PLACE_QUERY = re.compile(r"\b(координат\w*|gps|coordinates|latitude|longitude|адрес|address|"
                          r"къде се намира|where is|на картата|on the map)\b", re.IGNORECASE)


def _geocode(query: str) -> list[dict]:
    """Координатите на място от Nominatim (OpenStreetMap). Не намери ли пълната
    заявка — без последната дума („Широка лъка Родопи“ → „Широка лъка“): региони
    и планини не са част от адреса. Правилата на Nominatim: ≤ 1 заявка/сек."""
    words = _WIKI_NOISE.sub(" ", query).split()
    for attempt in range(3):
        q = " ".join(words[:len(words) - attempt])
        if not q:
            break
        if attempt:
            time.sleep(1.1)
        params = urllib.parse.urlencode({"q": q, "format": "json", "limit": 1, "accept-language": "bg"})
        try:
            data = json.loads(_http_get(f"https://nominatim.openstreetmap.org/search?{params}", timeout=8))
        except (ConnectionError, ValueError, OSError):
            return []
        if data:
            hit = data[0]
            lat, lon = float(hit["lat"]), float(hit["lon"])
            return [{"title": f"{hit.get('display_name', q)} — OpenStreetMap",
                     "url": f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=14/{lat:.5f}/{lon:.5f}",
                     "snippet": f"координати: {lat:.5f}, {lon:.5f} (Nominatim, по заявка „{q}“)"}]
    return []


def _wikipedia_search(query: str, max_results: int = 5) -> list[dict]:
    """Резервата: Wikipedia API (кирилица → първо bg, после en). Резултатите
    носят първите изречения на статията и координатите, ако е място."""
    langs = ("bg", "en") if re.search(r"[а-яА-Я]", query) else ("en", "bg")
    # Думите за КАКВО се търси (координати, GPS…) не са в статията за мястото —
    # с тях „Широка лъка координати“ даваше реки и хижи, но не и селото.
    query = _WIKI_NOISE.sub(" ", query).strip() or query
    out: list[dict] = []
    for lang in langs:
        params = urllib.parse.urlencode({
            "action": "query", "generator": "search", "gsrsearch": query, "gsrlimit": max_results,
            "prop": "coordinates|extracts", "exintro": 1, "explaintext": 1, "exsentences": 2,
            "exlimit": max_results, "format": "json", "formatversion": 2,
        })
        try:
            data = json.loads(_http_get(f"https://{lang}.wikipedia.org/w/api.php?{params}", timeout=8))
        except (ConnectionError, ValueError, OSError):
            continue
        pages = sorted((data.get("query") or {}).get("pages") or [], key=lambda pg: pg.get("index", 99))
        for pg in pages:
            title = pg.get("title", "")
            snippet = " ".join((pg.get("extract") or "").split())[:280]
            coords = (pg.get("coordinates") or [{}])[0]
            if "lat" in coords:
                snippet = f"координати: {coords['lat']:.5f}, {coords['lon']:.5f}. {snippet}"
            out.append({"title": f"{title} — Wikipedia ({lang})",
                        "url": f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
                        "snippet": snippet})
        if out:
            break
    return out[:max_results]


# ─── Page Fetcher ─────────────────────────────────────────────────────────────

def fetch_page(url: str, *, use_cache: bool = True, max_chars: int = 5000) -> str:
    """
    Извлича и почиства текстовото съдържание на уебстраница.

    Args:
        url:       URL за четене.
        use_cache: Дали да ползва кеш.
        max_chars: Максимален брой символи в резултата.

    Returns:
        Чист текст от страницата.
    """
    cache_key = _cache_key(f"page:{url}")
    if use_cache:
        cached = _cache_get(cache_key)
        if cached:
            return cached[:max_chars]

    try:
        html = _http_get(url, timeout=12)
        text = _extract_text(html)
        # Почистваме двойни интервали
        text = re.sub(r"\s{3,}", "  ", text).strip()
        result = text[:max_chars]
    except ConnectionError as e:
        log.error(f"[web_search] Страницата недостъпна: {e}")
        result = f"Грешка: {e}"

    if use_cache and result:
        _cache_set(cache_key, result)

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("🔍 Тест: Търсене в DuckDuckGo...")
    results = search("python retry decorator asyncio", max_results=3)
    if results:
        for r in results:
            print(f"\n📌 {r['title']}")
            print(f"   🔗 {r['url']}")
            print(f"   📝 {r['snippet'][:150]}")
    else:
        print("⚠️  Не са намерени резултати (може да е мрежова грешка или DuckDuckGo смени HTML).")

    print("\n📄 Тест: Четене на страница...")
    content = fetch_page("https://docs.python.org/3/library/urllib.request.html", max_chars=500)
    print(content)
