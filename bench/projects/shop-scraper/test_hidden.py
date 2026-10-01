import pytest
from scraper import scrape


def product(href, name, price, old=None):
    old_html = f'<span class="old-price">{old}</span>' if old else ""
    return (f'<div class="product"><a class="title" href="{href}">{name}</a>'
            f'{old_html}<span class="price">{price}</span></div>')


def page(products, next_href=None):
    nav = f'<a rel="next" href="{next_href}">Следваща »</a>' if next_href else ""
    return (f'<html><body><div class="sidebar"><span class="price">0,01 лв.</span></div>'
            f'<main>{"".join(products)}</main><nav>{nav}</nav></body></html>')


SHOP = "https://shop.example.bg"
PAGES = {
    f"{SHOP}/catalog": page([
        product("/p/laptop-x1", "Лаптоп X1", "1 299,00 лв.", old="1 499,00 лв."),
        product(f"{SHOP}/p/mouse", "Мишка &amp; подложка", "24,90 €"),
    ], "?page=2"),
    f"{SHOP}/catalog?page=2": page([
        product("../p/kbd", "Клавиатура", "45,50 €"),
        product("/p/mouse", "Мишка &amp; подложка", "24,90 €"),
    ], "/catalog/page/3/"),
    f"{SHOP}/catalog/page/3/": page([
        product("cable-usb-c", "Кабел USB-C", "9,99 лв."),
    ], "page/4/"),
    f"{SHOP}/catalog/page/3/page/4/": page([
        product("/p/monitor", "Монитор 27\"", "2 345,67 лв."),
    ]),
}


def fake_fetch(seen):
    def fetch(url):
        seen.append(url)
        if url not in PAGES:
            raise AssertionError(f"неочакван адрес: {url}")
        return PAGES[url]
    return fetch


def test_all_pages_in_order():
    seen = []
    rows = scrape(f"{SHOP}/catalog", fetch=fake_fetch(seen))
    assert [r["name"] for r in rows] == [
        "Лаптоп X1", "Мишка & подложка", "Клавиатура", "Кабел USB-C", 'Монитор 27"']
    assert [r["url"] for r in rows] == [
        f"{SHOP}/p/laptop-x1", f"{SHOP}/p/mouse", f"{SHOP}/p/kbd",
        f"{SHOP}/catalog/page/3/cable-usb-c", f"{SHOP}/p/monitor"]
    assert seen == list(PAGES)


def test_prices_in_euro():
    rows = scrape(f"{SHOP}/catalog", fetch=fake_fetch([]))
    prices = {r["name"]: r["price"] for r in rows}
    assert prices["Лаптоп X1"] == pytest.approx(664.17)      # 1299 / 1.95583, not the old price
    assert prices["Мишка & подложка"] == pytest.approx(24.90)
    assert prices["Клавиатура"] == pytest.approx(45.50)
    assert prices["Кабел USB-C"] == pytest.approx(5.11)
    assert prices['Монитор 27"'] == pytest.approx(1199.32)
    assert all(isinstance(p, float) for p in prices.values())


def test_loop_stops():
    loop = {
        "https://a.bg/list": page([product("/x", "X", "1,00 €")], "/list?p=2"),
        "https://a.bg/list?p=2": page([product("/y", "Y", "2,00 €")], "https://a.bg/list"),
    }
    calls = []

    def fetch(url):
        calls.append(url)
        assert len(calls) < 10, "зацикли"
        return loop[url]

    rows = scrape("https://a.bg/list", fetch=fetch)
    assert [r["name"] for r in rows] == ["X", "Y"]
    assert calls == ["https://a.bg/list", "https://a.bg/list?p=2"]


def test_empty_page():
    assert scrape("https://a.bg/empty", fetch=lambda url: "<html><body>Няма продукти</body></html>") == []
