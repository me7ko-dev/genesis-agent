"""Еталонно решение — доказва, че скритите тестове се минават. Genesis не го вижда."""
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup


def _int(text):
    digits = re.sub(r"\D", "", text or "")
    return int(digits) if digits else None


def scrape(start_url, fetch, max_pages=50):
    ads, seen_ads, seen_pages = [], set(), set()
    url = start_url
    while url and url not in seen_pages and len(seen_pages) < max_pages:
        seen_pages.add(url)
        soup = BeautifulSoup(fetch(url), "html.parser")
        for div in soup.select("div.ad"):
            link = div.select_one("a.title")
            if link is None:
                continue
            ad_url = urljoin(url, link.get("href", ""))
            if ad_url in seen_ads:
                continue
            seen_ads.add(ad_url)
            price = div.select_one("span.price")
            year = div.select_one("span.year")
            ads.append({"title": link.get_text(strip=True), "url": ad_url,
                        "price": _int(price.get_text() if price else ""),
                        "year": _int(year.get_text() if year else "")})
        nxt = soup.select_one('a[rel="next"]')
        url = urljoin(url, nxt["href"]) if nxt and nxt.get("href") else None
    return ads
