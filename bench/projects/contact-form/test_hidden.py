import re
import sqlite3

import pytest
from app import create_app

GOOD = {"name": "Иван Петров", "email": "ivan@example.bg", "phone": "0888 123 456",
        "message": "Свободна ли е къщата на 12.10?"}


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "inq.db")


def client(db):
    app = create_app(db)
    app.config["TESTING"] = True
    return app.test_client()


def phones(db):
    with sqlite3.connect(db) as con:
        return [r[0] for r in con.execute("SELECT phone FROM inquiries")]


def test_the_page_has_a_labelled_form(db):
    html = client(db).get("/").get_data(as_text=True)
    assert re.search(r'<html[^>]*lang=["\']bg', html)
    assert re.search(r'<form[^>]*method=["\']?post', html, re.IGNORECASE) and "/contact" in html
    for field in ("name", "email", "phone", "message"):
        assert re.search(rf'name=["\']{field}["\']', html), field
    assert len(re.findall(r"<label", html)) >= 4


@pytest.mark.parametrize("raw", ["0888 123 456", "+359 888 123 456", "00359888123456"])
def test_valid_inquiry_is_stored_with_a_normalised_phone_and_redirects(db, raw):
    r = client(db).post("/contact", data={**GOOD, "phone": raw})
    assert r.status_code == 303 and r.headers["Location"].endswith("/thanks")
    assert phones(db) == ["+359888123456"]
    assert "Благодарим" in client(db).get("/thanks").get_data(as_text=True)


def test_phone_is_optional(db):
    assert client(db).post("/contact", data={**GOOD, "phone": ""}).status_code == 303
    assert phones(db) in ([""], [None])


@pytest.mark.parametrize("bad", [{"email": "ivan@"}, {"email": "ivan.bg"}, {"message": ""},
                                 {"name": "  "}, {"phone": "12345"}, {"phone": "0888 123 45"}])
def test_invalid_is_400_keeps_the_input_and_stores_nothing(db, bad):
    r = client(db).post("/contact", data={**GOOD, **bad})
    assert r.status_code == 400
    assert ("Иван Петров" in r.get_data(as_text=True)) or "name" in bad
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE IF NOT EXISTS inquiries (phone TEXT)")
        assert con.execute("SELECT count(*) FROM inquiries").fetchone()[0] == 0


def test_html_from_the_form_is_never_run(db):
    r = client(db).post("/contact", data={**GOOD, "name": "<script>alert(1)</script>", "email": "x@"})
    assert r.status_code == 400 and "<script>alert(1)</script>" not in r.get_data(as_text=True)


def test_data_survives_a_restart(db):
    client(db).post("/contact", data=GOOD)
    client(db).post("/contact", data={**GOOD, "phone": "+359 887 000 111"})
    assert phones(db) == ["+359888123456", "+359887000111"]
