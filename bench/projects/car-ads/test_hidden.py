from scraper import scrape

BASE = "https://koli.example.bg"


def ad(href, title, price, year):
    return (f'<div class="ad"><h2><a class="title" href="{href}">{title}</a></h2>'
            f'<p><span class="price">{price}</span> · <span class="year">{year}</span></p></div>')


def page(ads, next_href=None):
    nav = f'<nav><a href="/">Начало</a> <a rel="next" href="{next_href}">Следваща »</a></nav>' if next_href else "<nav></nav>"
    return f"<html><body><div class='ad-banner'>Реклама</div>{''.join(ads)}{nav}</body></html>"


class Site:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        return self.pages[url]


def test_follows_relative_next_links_and_resolves_urls():
    site = Site({
        f"{BASE}/obiavi": page([ad("/ad/1", "VW Golf 7", "12 500 €", "2015 г."),
                                ad("ad/2", " Opel Astra ", "Договаряне", "2011 г.")], "?page=2"),
        f"{BASE}/obiavi?page=2": page([ad(f"{BASE}/ad/3", "Toyota Yaris", "7 900 €", "2009 г.")],
                                      "/obiavi?page=3"),
        f"{BASE}/obiavi?page=3": page([ad("/ad/4", "Škoda Octavia", "1 250 €", "2003 г.")]),
    })
    ads = scrape(f"{BASE}/obiavi", site)
    assert ads == [
        {"title": "VW Golf 7", "url": f"{BASE}/ad/1", "price": 12500, "year": 2015},
        {"title": "Opel Astra", "url": f"{BASE}/ad/2", "price": None, "year": 2011},
        {"title": "Toyota Yaris", "url": f"{BASE}/ad/3", "price": 7900, "year": 2009},
        {"title": "Škoda Octavia", "url": f"{BASE}/ad/4", "price": 1250, "year": 2003},
    ]
    assert len(site.calls) == 3


def test_a_loop_back_ends_the_crawl():
    site = Site({
        f"{BASE}/a": page([ad("/ad/1", "A", "1 000 €", "2010 г.")], "/b"),
        f"{BASE}/b": page([ad("/ad/2", "B", "2 000 €", "2011 г.")], "/a"),
    })
    assert [a["title"] for a in scrape(f"{BASE}/a", site)] == ["A", "B"]
    assert len(site.calls) == 2


def test_the_same_ad_on_two_pages_counts_once():
    site = Site({
        f"{BASE}/p1": page([ad("/ad/1", "A", "1 000 €", "2010 г."), ad("/ad/2", "B", "2 000 €", "2011 г.")], "/p2"),
        f"{BASE}/p2": page([ad("/ad/2", "B", "2 000 €", "2011 г."), ad("/ad/3", "C", "3 000 €", "2012 г.")]),
    })
    assert [a["url"] for a in scrape(f"{BASE}/p1", site)] == [f"{BASE}/ad/{i}" for i in (1, 2, 3)]


def test_max_pages():
    pages = {f"{BASE}/p{i}": page([ad(f"/ad/{i}", f"Кола {i}", "5 000 €", "2015 г.")], f"/p{i + 1}")
             for i in range(1, 200)}
    site = Site(pages)
    assert len(scrape(f"{BASE}/p1", site)) == 50
    assert len(site.calls) == 50
    site = Site(pages)
    assert [a["title"] for a in scrape(f"{BASE}/p1", site, max_pages=3)] == ["Кола 1", "Кола 2", "Кола 3"]
    assert len(site.calls) == 3


def test_a_page_without_ads():
    site = Site({f"{BASE}/empty": "<html><body><p>Няма обяви</p></body></html>"})
    assert scrape(f"{BASE}/empty", site) == []
