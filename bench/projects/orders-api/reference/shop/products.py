from flask import Blueprint, jsonify

from . import db
from .validation import (
    check_name,
    check_price,
    fail,
    json_object,
    not_found,
    pagination,
)

bp = Blueprint("products", __name__)


def to_json(r):
    return {"id": r["id"], "name": r["name"], "price_cents": r["price_cents"]}


def load(pid):
    r = db.get().execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    if r is None:
        not_found(f"продукт {pid}")
    return r


@bp.post("/products")
def create():
    body = json_object()
    errors = {}
    name, price = check_name(body, errors), check_price(body, errors)
    if errors:
        fail(errors)
    con = db.get()
    with con:
        pid = con.execute("INSERT INTO products (name, price_cents) VALUES (?, ?)", (name, price)).lastrowid
    return jsonify(to_json(load(pid))), 201


@bp.get("/products")
def listing():
    return jsonify(db.page("products", [], pagination(), to_json))


@bp.get("/products/<int:pid>")
def one(pid):
    return jsonify(to_json(load(pid)))


@bp.patch("/products/<int:pid>")
def update(pid):
    load(pid)
    body = json_object()
    errors = {}
    changes = {}
    if "name" in body:
        changes["name"] = check_name(body, errors)
    if "price_cents" in body:
        changes["price_cents"] = check_price(body, errors)
    if errors:
        fail(errors)
    con = db.get()
    with con:
        for col, value in changes.items():
            con.execute(f"UPDATE products SET {col} = ? WHERE id = ?", (value, pid))
    return jsonify(to_json(load(pid)))
