import http.server
import threading

import pytest
from scraper import scrape


def _product(name, price, stock):
    return (f'<div class="product"><h3 class="name">{name}</h3>'
            f'<span class="price">{price}</span><span class="stock">{stock}</span></div>')


def _page(charset, body, nxt=None):
    link = f'<a class="next" href="{nxt}">Следваща »</a>' if nxt else ""
    return (f'<html><head><meta charset="{charset}"><title>Каталог</title></head><body>'
            f'{body}<nav class="pages">{link}</nav></body></html>')


MACHINE = _product("Кафемашина Delonghi", "<s>499,00 лв.</s> 449,00 лв.", "В наличност")
PAGES = {
    "/catalog": ("utf-8", _page("utf-8",
        '<section class="featured"><h2>Препоръчано</h2>' + MACHINE + "</section>"
        + _product("Чаша за кафе", "8,50 лв.", "В наличност")
        + _product("Кафе на зърна 1 кг", "32,90 лв.", "Изчерпан"), "?page=2")),
    "/catalog?page=2": ("windows-1251", _page("windows-1251",
        _product("Лаптоп Acer Aspire", "1 299,00 лв.", "В наличност")
        + MACHINE + _product("Мишка Logitech", "39,99 лв.", "Изчерпан"), "/catalog?page=3")),
    "/catalog?page=3": ("utf-8", _page("utf-8",
        _product('Монитор Dell 27"', "<s>620,00 лв.</s> 579,00 лв.", "В наличност"), "/catalog")),
}
EXPECTED = [
    {"name": "Кафемашина Delonghi", "price": 449.0, "in_stock": True},
    {"name": "Чаша за кафе", "price": 8.5, "in_stock": True},
    {"name": "Кафе на зърна 1 кг", "price": 32.9, "in_stock": False},
    {"name": "Лаптоп Acer Aspire", "price": 1299.0, "in_stock": True},
    {"name": "Мишка Logitech", "price": 39.99, "in_stock": False},
    {"name": 'Монитор Dell 27"', "price": 579.0, "in_stock": True},
]


class _Shop(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        _Shop.hits += 1
        page = PAGES.get(self.path)
        if page is None or _Shop.hits > 30:  # зациклил скрейпър не виси вечно
            self.send_error(404)
            return
        body = page[1].encode(page[0])
        self.send_response(200)
        self.send_header("Content-Type", "text/html")  # charset — само в <meta>
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def shop():
    _Shop.hits = 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Shop)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _scrape(url):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", scrape(url)), daemon=True)
    t.start()
    t.join(60)
    assert not t.is_alive(), "scrape() не приключи за 60 s — зацикля по „Следваща“"
    return out.get("r")


def test_all_pages_once_promo_price_cp1251_no_duplicates(shop):
    assert _scrape(shop + "/catalog") == EXPECTED
    assert _Shop.hits <= 6


def test_starting_from_a_middle_page_still_ends(shop):
    names = [p["name"] for p in _scrape(shop + "/catalog?page=2")]
    assert names == ["Лаптоп Acer Aspire", "Кафемашина Delonghi", "Мишка Logitech",
                     'Монитор Dell 27"', "Чаша за кафе", "Кафе на зърна 1 кг"]
