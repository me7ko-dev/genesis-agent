"""genesis_agent.web_search — когато търсачката върне CAPTCHA.

2026-09-28: DuckDuckGo (lite и html) и Mojeek връщаха на лаптопа само
„докажи, че си човек“. Тази страница излизаше като резултат и се кешираше;
моделът търси координатите на селото три пъти и накрая ги измисли (~30 km
встрани). Мрежа тук няма — _http_get е подменен."""
from __future__ import annotations

import json

from genesis_agent import web_search as ws

CAPTCHA = ("<html><body><div class='anomaly-modal'>Unfortunately, bots use DuckDuckGo too. "
           "Please complete the following challenge</div></body></html>")
WIKI = json.dumps({"query": {"pages": [
    {"index": 2, "title": "Широколъшка река", "extract": "Река в Южна България."},
    {"index": 1, "title": "Широка лъка", "extract": "Широка лъка е село в Южна България, област Смолян.",
     "coordinates": [{"lat": 41.6794, "lon": 24.5807}]},
]}})


def _fake_http(pages: dict[str, str], seen: list[str]):
    def get(url: str, timeout: int = 10) -> str:
        seen.append(url)
        for key, body in pages.items():
            if key in url:
                return body
        raise ConnectionError(url)
    return get


def test_a_captcha_falls_back_to_wikipedia(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(ws, "_http_get", _fake_http({"duckduckgo": CAPTCHA, "bg.wikipedia": WIKI}, seen))
    results = ws.search("Широка лъка координати", use_cache=False)
    assert results[0]["title"].startswith("Широка лъка")
    assert "41.67940, 24.58070" in results[0]["snippet"]
    assert not any("bots use" in r["snippet"] for r in results)
    wiki_url = next(u for u in seen if "wikipedia" in u)
    assert "%D0%BA%D0%BE%D0%BE%D1%80%D0%B4" not in wiki_url  # „координати“ не отива в Wikipedia


def test_the_captcha_is_never_cached(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(ws, "_http_get", _fake_http({"duckduckgo": CAPTCHA}, seen))
    first = ws.search("нещо рядко", use_cache=True)
    assert "Търсачката отказа" in first[0]["title"]
    assert "Не измисляй" in first[0]["snippet"]
    ws.search("нещо рядко", use_cache=True)
    assert sum("duckduckgo" in u for u in seen) == 2  # второто търсене пак пита, не взима от кеша


def test_normal_results_still_come_from_duckduckgo(monkeypatch) -> None:
    html = ('<a rel="nofollow" class="result-link" href="https://example.org/a">Първи резултат</a>'
            "<td class='result-snippet'>описание</td>")
    seen: list[str] = []
    monkeypatch.setattr(ws, "_http_get", _fake_http({"duckduckgo": html}, seen))
    results = ws.search("заявка", use_cache=False)
    assert results[0]["url"] == "https://example.org/a"
    assert not any("wikipedia" in u for u in seen)
