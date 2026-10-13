from flask import Blueprint, jsonify

from . import db
from .validation import (
    EMAIL,
    check_name,
    conflict,
    fail,
    json_object,
    not_found,
    pagination,
)

bp = Blueprint("customers", __name__)


def to_json(r):
    return {"id": r["id"], "name": r["name"], "email": r["email"]}


def load(cid):
    r = db.get().execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if r is None:
        not_found(f"клиент {cid}")
    return r


@bp.post("/customers")
def create():
    body = json_object()
    errors = {}
    name = check_name(body, errors)
    email = body.get("email")
    email = email.strip().lower() if isinstance(email, str) else ""
    if not EMAIL.match(email):
        errors["email"] = "невалиден имейл"
    if errors:
        fail(errors)
    con = db.get()
    if con.execute("SELECT 1 FROM customers WHERE email = ?", (email,)).fetchone():
        conflict(f"вече има клиент с имейл {email}")
    with con:
        cid = con.execute("INSERT INTO customers (name, email) VALUES (?, ?)", (name, email)).lastrowid
    return jsonify(to_json(load(cid))), 201


@bp.get("/customers")
def listing():
    return jsonify(db.page("customers", [], pagination(), to_json))


@bp.get("/customers/<int:cid>")
def one(cid):
    return jsonify(to_json(load(cid)))
