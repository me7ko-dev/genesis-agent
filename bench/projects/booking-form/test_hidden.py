import re
from datetime import date, timedelta

import pytest
from app import create_app

TOMORROW = (date.today() + timedelta(days=1)).isoformat()
ALL_SLOTS = [f"{h:02d}:{m:02d}" for h in range(9, 18) for m in (0, 30)]


@pytest.fixture
def client(tmp_path):
    return create_app(str(tmp_path / "b.db")).test_client()


def form(**kw):
    data = {"name": "Иван Петров", "phone": "0888 123 456", "date": TOMORROW,
            "time": "10:00", "service": "Подстригване"}
    data.update(kw)
    return data


def test_page_has_the_form(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert re.search(r"<form[^>]*method=[\"']?post", html, re.IGNORECASE)
    assert re.search(r"<form[^>]*action=[\"']?/book", html, re.IGNORECASE)
    for field in ("name", "phone", "date", "time", "service"):
        assert re.search(rf"name=[\"']?{field}\b", html), field
    assert html.lower().count("<label") >= 5
    for service in ("Подстригване", "Боядисване", "Прическа"):
        assert service in html


def test_booking_redirects_to_thanks(client):
    r = client.post("/book", data=form())
    assert r.status_code == 303
    assert r.headers["Location"].endswith("/thanks")
    assert client.get("/thanks").status_code == 200


@pytest.mark.parametrize("phone", ["0888123456", "+359 888 123 456", "0898/123-456"])
def test_mobile_formats(client, phone):
    assert client.post("/book", data=form(phone=phone)).status_code == 303


@pytest.mark.parametrize("bad", [
    {"name": ""},
    {"phone": ""},
    {"phone": "02 987 6543"},          # стационарен, не мобилен
    {"phone": "0888 12"},
    {"date": (date.today() - timedelta(days=1)).isoformat()},
    {"date": "утре"},
    {"time": "08:30"},
    {"time": "18:00"},
    {"time": "10:15"},
    {"service": ""},
    {"service": "Маникюр"},
])
def test_rejected(client, bad):
    r = client.post("/book", data=form(**bad))
    assert r.status_code == 400
    assert "<form" in r.get_data(as_text=True).lower()


def test_error_keeps_what_was_typed(client):
    r = client.post("/book", data=form(name="Мария Георгиева", time="18:00"))
    assert r.status_code == 400
    assert "Мария Георгиева" in r.get_data(as_text=True)


def test_taken_slot(client):
    assert client.post("/book", data=form()).status_code == 303
    assert client.post("/book", data=form(name="Друг", phone="0899 111 222")).status_code == 400
    assert client.post("/book", data=form(time="10:30")).status_code == 303


def test_free_slots(client):
    assert client.get(f"/api/slots?date={TOMORROW}").get_json() == ALL_SLOTS
    client.post("/book", data=form(time="09:00"))
    client.post("/book", data=form(time="17:30"))
    assert client.get(f"/api/slots?date={TOMORROW}").get_json() == ALL_SLOTS[1:-1]


def test_bookings_survive_a_restart(tmp_path):
    db = str(tmp_path / "p.db")
    create_app(db).test_client().post("/book", data=form(time="12:00"))
    slots = create_app(db).test_client().get(f"/api/slots?date={TOMORROW}").get_json()
    assert "12:00" not in slots and len(slots) == len(ALL_SLOTS) - 1
