"""genesis_agent.search_browser — истински браузър (без прозорец) за търсене.

Защо (мерено на лаптопа 2026-11-17): търсачките отказват заявки „без браузър“.
Bing връща чужди резултати („височина връх Мусала“ → аптека с „Аденурик“, и на
английски — Outlook/НАП), DuckDuckGo html/lite дава CAPTCHA (HTTP 202) на
кирилица, Brave — 429, Google — страница без резултати. През Edge без прозорец
DuckDuckGo и Brave дадоха верните страници за 1.4–2.4 s, на кирилица също.

И браузърът не е безкраен: след ~10 проби подред Brave показа „проверяваме дали
не сте бот“, а DuckDuckGo — празна страница. Затова тук всяка търсачка, която
откаже, почива (search_engines.cool_down), и браузърът е стъпка след API-тата,
не първа.

Браузърът живее в отделна нишка с asyncio цикъл: стартът (~1 s) се плаща веднъж
на сесия, а търсачките и страниците се отварят паралелно в отделни раздели.
Профилът е празен (без бисквитките/паролите на потребителя). Вътрешни адреси
и localhost не се отварят, и то за всяка заявка на страницата (_refusal).
"""
from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import logging
import os
import threading
import time
import urllib.parse
from typing import Any

log = logging.getLogger("genesis.search_browser")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# Неуспешен старт (няма Playwright/браузър) не се опитва при всяко търсене.
_RETRY_START_AFTER = 600.0
_HEAVY = frozenset({"image", "media", "font"})
_BOT_WALL = ("captcha", "проверяваме дали не сте бот", "not a robot", "unusual traffic",
             "bots use duckduckgo", "anomaly-modal")


class Blocked(Exception):
    """Търсачката показа „докажи, че си човек“ — да почива."""


def enabled() -> bool:
    """GENESIS_SEARCH_BROWSER=0 изключва браузъра (тестовете, сървър без браузър)."""
    return os.environ.get("GENESIS_SEARCH_BROWSER", "1").strip() not in ("0", "false", "no", "off")


def _channels() -> list[str]:
    """Кой браузър да пробваме: изрично зададеният, вграденият Chromium на
    Playwright, после Edge и Chrome от системата. На лаптопа (2026-11-17)
    вграденият липсва (`playwright install` не е пускан), а Edge го има винаги
    на Windows — без него BROWSE и търсенето в браузъра не тръгваха изобщо."""
    forced = os.environ.get("GENESIS_BROWSER_CHANNEL", "").strip()
    order = [forced] if forced else []
    order += ["", "msedge", "chrome"]
    seen: list[str] = []
    for c in order:
        if c not in seen:
            seen.append(c)
    return seen


_REFUSAL_TTL = 60.0
_refusals: dict[str, tuple[float, str]] = {}


def _refusal(url: str) -> str:
    """Защо браузърът за търсене не бива да пусне заявка към url („“ — може).

    Преглед за сигурност 2026-11-17: тук имаше browser._address_refusal, а тя
    чете browser._allow_loopback — след BROWSE към localhost и страниците от
    търсенето можеха към localhost. Тези страници са чужди и се отварят без
    човек, затова loopback е забранен винаги. Името се разрешава за ВСЯКА
    заявка (не само за „вътрешни по вид“ адреси, както в BROWSE): публично име,
    което сочи към 192.168.x.x, иначе минаваше. Кеш по хост за 60 s — една
    страница прави десетки заявки към няколко хоста."""
    from genesis_agent import web_fetch
    if not url.lower().startswith(("http://", "https://")):
        return ""   # about:blank, data:, blob: — не са мрежови адреси
    try:
        host = web_fetch._check_url(url)
    except web_fetch.FetchError as e:
        return str(e)
    except (ValueError, OSError) as e:
        return f"невалиден адрес: {e}"
    now = time.monotonic()
    hit = _refusals.get(host)
    if hit and now - hit[0] < _REFUSAL_TTL:
        return hit[1]
    try:
        web_fetch._precheck_through_proxy(host, url, False)
        why = ""
    except web_fetch.FetchError as e:
        why = str(e)
    except (ValueError, OSError) as e:
        why = f"невалиден адрес: {e}"
    _refusals[host] = (now, why)
    return why


async def _route(route: Any) -> None:
    """Картинки/шрифтове/видео не ни трябват (по-бързо); вътрешни адреси — не."""
    req = route.request
    try:
        if req.resource_type in _HEAVY:
            await route.abort()
            return
        refusal = await asyncio.get_running_loop().run_in_executor(None, _refusal, req.url)
        if refusal:
            log.info("търсене в браузъра: спряна заявка към %s — %s", req.url[:200], refusal)
            await route.abort("blockedbyclient")
            return
        await route.continue_()
    except Exception:
        log.debug("route handler-ът не довърши заявката", exc_info=True)


class _Runner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pw: Any = None
        self._browser: Any = None
        self._ctx: Any = None
        self._failed_at = 0.0
        self.channel = ""

    def available(self) -> bool:
        if not enabled():
            return False
        if self._failed_at and time.monotonic() - self._failed_at < _RETRY_START_AFTER:
            return False
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False
        return True

    def run(self, coro_factory: Any, timeout: float) -> Any:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, name="genesis-search-browser",
                                 daemon=True).start()
                self._loop = loop
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:  # на 3.10 не е вграденият TimeoutError
            fut.cancel()
            raise

    async def context(self) -> Any:
        if self._ctx is not None:
            return self._ctx
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        last: Exception | None = None
        for channel in _channels():
            try:
                kw: dict[str, Any] = {"headless": True}
                if channel:
                    kw["channel"] = channel
                self._browser = await self._pw.chromium.launch(**kw)
                self.channel = channel or "chromium"
                break
            except Exception as e:  # noqa: BLE001 — пробва следващия канал; последната грешка излиза по-долу
                last = e
        if self._browser is None:
            await self._pw.stop()
            self._pw = None
            self._failed_at = time.monotonic()
            raise RuntimeError(f"няма браузър за търсене: {last}")
        self._ctx = await self._browser.new_context(locale="bg-BG", user_agent=_UA)
        await self._ctx.route("**/*", _route)
        log.info("браузърът за търсене тръгна (%s)", self.channel)
        return self._ctx

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
        except Exception:
            log.debug("браузърът за търсене не се затвори чисто", exc_info=True)
        self._pw = self._browser = self._ctx = None


_runner = _Runner()


def available() -> bool:
    return _runner.available()


def _shutdown() -> None:
    if _runner._loop is not None and _runner._ctx is not None:
        try:
            _runner.run(_runner.close, 5)
        except Exception:
            log.debug("затварянето на браузъра за търсене се провали", exc_info=True)


atexit.register(_shutdown)


# ─── Търсачки ────────────────────────────────────────────────────────────────

# Всяка резултатна „карта“: първата външна връзка е адресът, заглавието е
# h2/h3 или текстът на връзката, описанието — останалият текст на картата.
_EXTRACT_JS = """
([cardSel, host]) => {
  const out = [];
  for (const card of document.querySelectorAll(cardSel)) {
    const links = [...card.querySelectorAll('a[href^="http"]')]
      .filter(a => !a.hostname.endsWith(host));
    if (!links.length) continue;
    const head = card.querySelector('h2, h3, .title, [data-testid="result-title-a"]');
    const title = ((head && head.innerText) || links[0].innerText || '').trim();
    const snipEl = card.querySelector('[data-result="snippet"], .snippet-description, .generic-snippet, .content');
    let snippet = ((snipEl && snipEl.innerText) || card.innerText || '').trim();
    if (snippet.startsWith(title)) snippet = snippet.slice(title.length).trim();
    out.push({title, url: links[0].href, snippet: snippet.replace(/\\s+/g, ' ').slice(0, 400)});
  }
  return out;
}
"""

_ENGINES: dict[str, tuple[str, str, str]] = {
    # име: (адрес с {q}, селектор на карта, собствен домейн)
    "ddg_browser": ("https://duckduckgo.com/?q={q}&kl={kl}", 'article[data-testid="result"]', "duckduckgo.com"),
    "brave_browser": ("https://search.brave.com/search?q={q}", '#results .snippet[data-type="web"]', "brave.com"),
}


async def _search_async(engine: str, query: str, max_results: int, region: str) -> list[dict]:
    url_t, card_sel, host = _ENGINES[engine]
    ctx = await _runner.context()
    page = await ctx.new_page()
    try:
        url = url_t.format(q=urllib.parse.quote(query), kl=urllib.parse.quote(region))
        await page.goto(url, wait_until="domcontentloaded", timeout=12000)
        try:
            await page.wait_for_selector(card_sel, timeout=6000)
        except Exception:  # noqa: BLE001 — няма резултати навреме: стена за ботове или празна страница
            text = ((await page.title()) + " " + (await page.inner_text("body"))[:3000]).lower()
            if any(m in text for m in _BOT_WALL):
                raise Blocked(engine) from None
            return []
        rows = await page.evaluate(_EXTRACT_JS, [card_sel, host])
        return [r for r in rows if r.get("url")][:max_results]
    finally:
        await page.close()


def search(engine: str, query: str, max_results: int = 8, *, region: str = "bg-bg",
           timeout: float = 15.0) -> list[dict]:
    """Резултатите от търсачка през браузъра. Хвърля Blocked при CAPTCHA."""
    if not _runner.available():
        return []
    return _runner.run(lambda: _search_async(engine, query, max_results, region), timeout)


async def _fetch_async(url: str) -> tuple[str, str, str]:
    ctx = await _runner.context()
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=12000)
        await page.wait_for_timeout(700)  # страници, които рисуват текста с JS
        if _refusal(page.url):
            return page.url, "", ""
        return page.url, await page.title(), await page.inner_text("body")
    finally:
        await page.close()


def fetch_text(url: str, *, timeout: float = 15.0) -> tuple[str, str, str]:
    """(краен адрес, заглавие, текст) на страница, рисувана с JavaScript."""
    if _refusal(url) or not _runner.available():
        return url, "", ""
    return _runner.run(lambda: _fetch_async(url), timeout)
