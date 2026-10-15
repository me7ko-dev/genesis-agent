from datetime import date, timedelta

import pytest
from app import create_app
from bs4 import BeautifulSoup


def day(n):
    return (date.today() + timedelta(days=n)).isoformat()


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "b.db"))
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    return app.test_client()


def form(**over):
    data = {"name": "Иван Петров", "phone": "0888 123 456", "check_in": day(10),
            "check_out": day(13), "guests": "2"}
    data.update(over)
    return data


def book(client, **over):
    return client.post("/book", data=form(**over))


def text(r):
    return BeautifulSoup(r.get_data(as_text=True), "html.parser").get_text(" ")


def test_form_page(client):
    r = client.get("/")
    assert r.status_code == 200
    soup = BeautifulSoup(r.get_data(as_text=True), "html.parser")
    f = soup.find("form", attrs={"action": "/book"})
    assert f is not None and f.get("method", "").lower() == "post"
    types = {"check_in": "date", "check_out": "date", "guests": "number"}
    for name in ("name", "phone", "check_in", "check_out", "guests"):
        field = f.find(attrs={"name": name})
        assert field is not None, name
        if name in types:
            assert field.get("type") == types[name], name
        assert field.get("id") and soup.find("label", attrs={"for": field["id"]}), name
    assert f.find(["button", "input"], attrs={"type": "submit"}) or f.find("button")


def test_booking_and_thanks(client):
    r = book(client)
    assert r.status_code == 303
    loc = r.headers["Location"]
    assert "/thanks/" in loc
    page = client.get(loc)
    assert page.status_code == 200
    body = text(page)
    assert "Иван Петров" in body
    assert day(10) in page.get_data(as_text=True) or date.fromisoformat(day(10)).strftime("%d.%m.%Y") in body


@pytest.mark.parametrize("over", [
    {"name": ""}, {"phone": ""}, {"check_in": ""}, {"check_out": ""}, {"guests": ""},
    {"check_in": day(-1), "check_out": day(2)},
    {"check_in": day(5), "check_out": day(5)},
    {"check_in": day(5), "check_out": day(4)},
    {"guests": "0"}, {"guests": "7"}, {"guests": "две"},
    {"phone": "0288123456"}, {"phone": "+359 88 123"}, {"phone": "0888 12345"},
])
def test_invalid(client, over):
    r = book(client, **over)
    assert r.status_code == 400
    assert client.get("/api/booked").get_json() == []


def test_errors_keep_values(client):
    r = book(client, guests="9", name="Мария Иванова", phone="+359 877 112 233")
    assert r.status_code == 400
    soup = BeautifulSoup(r.get_data(as_text=True), "html.parser")
    assert soup.find(attrs={"name": "name"}).get("value") == "Мария Иванова"
    assert soup.find(attrs={"name": "phone"}).get("value") == "+359 877 112 233"
    assert soup.find(attrs={"name": "check_in"}).get("value") == day(10)


@pytest.mark.parametrize("phone", ["0888123456", "0888 123 456", "+359888123456", "+359 87 711 2233"])
def test_valid_phones(client, phone):
    assert book(client, phone=phone).status_code == 303


def test_overlap(client):
    assert book(client, check_in=day(10), check_out=day(13)).status_code == 303
    for ci, co in [(day(11), day(12)), (day(9), day(11)), (day(12), day(15)),
                   (day(8), day(20)), (day(10), day(13))]:
        r = book(client, check_in=ci, check_out=co)
        assert r.status_code == 409, (ci, co)
        assert "Тези дати са заети" in text(r)
    assert book(client, check_in=day(13), check_out=day(15)).status_code == 303
    assert book(client, check_in=day(7), check_out=day(10)).status_code == 303
    assert client.get("/api/booked").get_json() == [
        {"check_in": day(7), "check_out": day(10)},
        {"check_in": day(10), "check_out": day(13)},
        {"check_in": day(13), "check_out": day(15)},
    ]


def test_no_html_injection(client):
    evil = '<script>alert(1)</script><b id="x">Иван</b>'
    r = book(client, name=evil)
    assert r.status_code == 303
    html = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert BeautifulSoup(html, "html.parser").find(id="x") is None
    err = book(client, name=evil, guests="0").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in err


def test_api_has_no_personal_data(client):
    book(client, name="Тайно Име", phone="0899 999 999")
    raw = client.get("/api/booked").get_data(as_text=True)
    assert "Тайно" not in raw and "0899" not in raw and "999 999" not in raw


def test_persists(tmp_path):
    db = str(tmp_path / "p.db")
    c1 = create_app(db).test_client()
    assert c1.post("/book", data=form()).status_code == 303
    c2 = create_app(db).test_client()
    assert c2.get("/api/booked").get_json() == [{"check_in": day(10), "check_out": day(13)}]
    assert c2.post("/book", data=form()).status_code == 409
