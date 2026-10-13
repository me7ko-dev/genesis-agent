from pathlib import Path

import pytest
from shop import create_app


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "shop.db")


@pytest.fixture
def c(db_path):
    return create_app(db_path).test_client()


def product(c, name="Кафе", price=1000):
    r = c.post("/products", json={"name": name, "price_cents": price})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def customer(c, name="Ани", email="ani@example.bg"):
    r = c.post("/customers", json={"name": name, "email": email})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def order(c, cid, lines):
    r = c.post("/orders", json={"customer_id": cid, "lines": lines})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def test_it_is_a_package_with_separate_modules():
    import shop
    pkg = Path(shop.__file__).parent
    for name in ("__init__", "db", "validation", "pricing", "products", "customers", "orders", "summary"):
        assert (pkg / f"{name}.py").is_file(), name


def test_product_create_get_patch(c):
    p = product(c, "Чай", 450)
    assert p == {"id": p["id"], "name": "Чай", "price_cents": 450}
    assert c.get(f"/products/{p['id']}").get_json() == p
    r = c.patch(f"/products/{p['id']}", json={"price_cents": 500})
    assert r.status_code == 200 and r.get_json()["price_cents"] == 500 and r.get_json()["name"] == "Чай"


@pytest.mark.parametrize("price", [True, 12.5, "100", 0, -5, None])
def test_price_must_be_a_positive_integer(c, price):
    r = c.post("/products", json={"name": "Х", "price_cents": price})
    assert r.status_code == 400
    assert "price_cents" in r.get_json()["errors"]


def test_all_errors_at_once(c):
    r = c.post("/products", json={"name": "  ", "price_cents": "x"})
    assert r.status_code == 400
    assert set(r.get_json()["errors"]) >= {"name", "price_cents"}
    r = c.post("/customers", json={"name": "", "email": "няма-маймунка"})
    assert set(r.get_json()["errors"]) >= {"name", "email"}


@pytest.mark.parametrize("data", ["не е json", "[1, 2]", "42"])
def test_body_that_is_not_a_json_object(c, data):
    r = c.post("/products", data=data, content_type="application/json")
    assert r.status_code == 400 and r.is_json


def test_patch_checks_and_404(c):
    p = product(c)
    assert c.patch(f"/products/{p['id']}", json={"price_cents": 0}).status_code == 400
    assert c.get(f"/products/{p['id']}").get_json()["price_cents"] == 1000
    r = c.patch("/products/999", json={"name": "X"})
    assert r.status_code == 404 and "error" in r.get_json()
    assert c.get("/products/999").status_code == 404


def test_email_normalized_and_unique_regardless_of_case(c):
    a = customer(c, email="  Ani@Example.BG ")
    assert a["email"] == "ani@example.bg"
    r = c.post("/customers", json={"name": "Друга", "email": "ANI@example.bg"})
    assert r.status_code == 409 and "error" in r.get_json()
    assert c.get(f"/customers/{a['id']}").get_json() == a
    assert c.get("/customers/999").status_code == 404


@pytest.mark.parametrize("email", ["ani@", "@example.bg", "ani@example", "a ni@example.bg", "ani@@x.bg", 5])
def test_bad_email(c, email):
    r = c.post("/customers", json={"name": "Ани", "email": email})
    assert r.status_code == 400 and "email" in r.get_json()["errors"]


def test_pagination(c):
    ids = [product(c, f"П{i}", 100 + i)["id"] for i in range(25)]
    first = c.get("/products").get_json()
    assert first["page"] == 1 and first["per_page"] == 20 and first["total"] == 25
    assert [p["id"] for p in first["items"]] == ids[:20]
    second = c.get("/products?page=2&per_page=20").get_json()
    assert [p["id"] for p in second["items"]] == ids[20:]
    r = c.get("/products?page=9")
    assert r.status_code == 200 and r.get_json()["items"] == [] and r.get_json()["total"] == 25
    assert [p["id"] for p in c.get("/products?page=3&per_page=7").get_json()["items"]] == ids[14:21]


@pytest.mark.parametrize("query", ["page=0", "page=-1", "per_page=0", "per_page=101", "page=abc", "per_page=1.5"])
def test_bad_pagination(c, query):
    for url in ("/products", "/customers", "/orders"):
        assert c.get(f"{url}?{query}").status_code == 400, url


def test_order_snapshot_merge_and_vat(c):
    cid = customer(c)["id"]
    coffee, tea = product(c, "Кафе", 1999), product(c, "Чай", 450)
    o = order(c, cid, [{"product_id": coffee["id"], "qty": 2}, {"product_id": tea["id"], "qty": 1},
                       {"product_id": coffee["id"], "qty": 1}])
    assert o["status"] == "new" and o["customer_id"] == cid
    assert o["lines"] == [
        {"product_id": coffee["id"], "name": "Кафе", "qty": 3, "unit_price_cents": 1999, "net_cents": 5997},
        {"product_id": tea["id"], "name": "Чай", "qty": 1, "unit_price_cents": 450, "net_cents": 450},
    ]
    assert (o["net_cents"], o["vat_cents"], o["total_cents"]) == (6447, 1289, 7736)
    c.patch(f"/products/{coffee['id']}", json={"price_cents": 5000, "name": "Ново кафе"})
    assert c.get(f"/orders/{o['id']}").get_json() == o


def test_vat_once_on_the_total_not_per_line(c):
    cid = customer(c)["id"]
    a, b = product(c, "А", 3), product(c, "Б", 3)
    o = order(c, cid, [{"product_id": a["id"], "qty": 1}, {"product_id": b["id"], "qty": 1}])
    # 6 ст. × 20% = 1,2 → 1 (по редове щеше да е 1 + 1 = 2)
    assert (o["net_cents"], o["vat_cents"], o["total_cents"]) == (6, 1, 7)
    o = order(c, cid, [{"product_id": a["id"], "qty": 1}])
    assert (o["net_cents"], o["vat_cents"], o["total_cents"]) == (3, 1, 4)   # 0,6 → 1


@pytest.mark.parametrize(("body", "field"), [
    ({"customer_id": 999, "lines": "OK"}, "customer_id"),
    ({"customer_id": "1", "lines": "OK"}, "customer_id"),
    ({"customer_id": "C", "lines": []}, "lines"),
    ({"customer_id": "C", "lines": "x"}, "lines"),
    ({"customer_id": "C", "lines": [{"product_id": 999, "qty": 1}]}, "lines"),
    ({"customer_id": "C", "lines": [{"product_id": "P", "qty": 0}]}, "lines"),
    ({"customer_id": "C", "lines": [{"product_id": "P", "qty": 1.5}]}, "lines"),
    ({"customer_id": "C", "lines": [{"product_id": "P", "qty": True}]}, "lines"),
    ({"customer_id": "C", "lines": [{"product_id": "P"}]}, "lines"),
])
def test_bad_order(c, body, field):
    cid, pid = customer(c)["id"], product(c)["id"]
    body = dict(body)
    if body["customer_id"] == "C":
        body["customer_id"] = cid
    if body["lines"] == "OK":
        body["lines"] = [{"product_id": pid, "qty": 1}]
    elif isinstance(body["lines"], list):
        body["lines"] = [{k: (pid if v == "P" else v) for k, v in ln.items()} for ln in body["lines"]]
    r = c.post("/orders", json=body)
    assert r.status_code == 400 and field in r.get_json()["errors"], r.get_json()
    assert c.get("/orders").get_json()["total"] == 0


def test_bad_customer_and_bad_lines_reported_together(c):
    r = c.post("/orders", json={"customer_id": 999, "lines": []})
    assert r.status_code == 400 and set(r.get_json()["errors"]) >= {"customer_id", "lines"}


def test_status_transitions(c):
    cid, pid = customer(c)["id"], product(c)["id"]

    def new():
        return order(c, cid, [{"product_id": pid, "qty": 1}])["id"]

    def move(oid, status):
        return c.post(f"/orders/{oid}/status", json={"status": status})

    o = new()
    r = move(o, "paid")
    assert r.status_code == 200 and r.get_json()["status"] == "paid"
    assert move(o, "shipped").get_json()["status"] == "shipped"
    for status in ("cancelled", "paid", "new", "shipped"):
        assert move(o, status).status_code == 409, status
    o = new()
    assert move(o, "shipped").status_code == 409
    assert move(o, "new").status_code == 409
    assert move(o, "cancelled").status_code == 200
    assert move(o, "paid").status_code == 409
    o = new()
    move(o, "paid")
    assert move(o, "cancelled").status_code == 200
    r = move(new(), "lost")
    assert r.status_code == 400 and "status" in r.get_json()["errors"]
    assert move(999, "paid").status_code == 404


def test_orders_filters(c):
    a, b = customer(c)["id"], customer(c, "Боби", "bobi@example.bg")["id"]
    pid = product(c)["id"]
    o1 = order(c, a, [{"product_id": pid, "qty": 1}])["id"]
    o2 = order(c, b, [{"product_id": pid, "qty": 1}])["id"]
    o3 = order(c, a, [{"product_id": pid, "qty": 1}])["id"]
    c.post(f"/orders/{o3}/status", json={"status": "paid"})
    ids = lambda q: [o["id"] for o in c.get(f"/orders{q}").get_json()["items"]]
    assert ids("") == [o1, o2, o3]
    assert ids(f"?customer_id={a}") == [o1, o3]
    assert ids("?status=new") == [o1, o2]
    assert ids(f"?status=paid&customer_id={a}") == [o3]
    assert c.get("/orders?status=new").get_json()["total"] == 2
    assert c.get("/orders?status=lost").status_code == 400


def test_summary(c):
    cid = customer(c)["id"]
    x, y, z, w = (product(c, n, p)["id"] for n, p in (("X", 100), ("Y", 200), ("Z", 300), ("W", 50)))
    empty = c.get("/summary").get_json()
    assert empty == {"orders": 0, "by_status": {"new": 0, "paid": 0, "shipped": 0, "cancelled": 0},
                     "revenue_cents": 0, "top_products": []}

    def make(lines, *statuses):
        o = order(c, cid, [{"product_id": p, "qty": q} for p, q in lines])
        for s in statuses:
            assert c.post(f"/orders/{o['id']}/status", json={"status": s}).status_code == 200
        return o

    o1 = make([(x, 2), (y, 1)], "paid")                    # 400 + ДДС 80 = 480
    o2 = make([(z, 2), (w, 5)], "paid", "shipped")         # 850 + 170 = 1020
    make([(x, 50)])                                         # нова — не се брои за приходи/топ
    make([(y, 99)], "paid", "cancelled")                   # отказана
    make([(w, 1)], "cancelled")
    s = c.get("/summary").get_json()
    assert s["by_status"] == {"new": 1, "paid": 1, "shipped": 1, "cancelled": 2}
    assert s["orders"] == 3
    assert s["revenue_cents"] == o1["total_cents"] + o2["total_cents"] == 1500
    # W: 5, X: 2, Z: 2 (равни → по product_id), Y: 1 — само платени и изпратени
    assert s["top_products"] == [{"product_id": w, "name": "W", "qty": 5},
                                 {"product_id": x, "name": "X", "qty": 2},
                                 {"product_id": z, "name": "Z", "qty": 2}]


def test_data_survives_a_restart(db_path):
    c1 = create_app(db_path).test_client()
    cid, pid = customer(c1)["id"], product(c1)["id"]
    o = order(c1, cid, [{"product_id": pid, "qty": 2}])
    c2 = create_app(db_path).test_client()
    assert c2.get(f"/orders/{o['id']}").get_json() == o
    assert c2.post("/customers", json={"name": "X", "email": "ANI@example.bg"}).status_code == 409


def test_every_answer_is_json(c):
    for r in (c.get("/products/999"), c.get("/orders/999"), c.get("/customers/abc"),
              c.post("/orders", data="x", content_type="text/plain")):
        assert r.is_json, r.status_code
