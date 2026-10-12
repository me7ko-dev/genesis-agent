from flask import Blueprint, jsonify, request

from . import db
from .pricing import totals
from .validation import conflict, fail, is_int, json_object, not_found, pagination

bp = Blueprint("orders", __name__)

STATUSES = ("new", "paid", "shipped", "cancelled")
ALLOWED = {("new", "paid"), ("new", "cancelled"), ("paid", "shipped"), ("paid", "cancelled")}


def to_json(r):
    lines = db.get().execute(
        "SELECT product_id, name, qty, unit_price_cents, net_cents FROM order_lines "
        "WHERE order_id = ? ORDER BY pos", (r["id"],)).fetchall()
    return {"id": r["id"], "customer_id": r["customer_id"], "status": r["status"],
            "lines": [dict(line) for line in lines],
            "net_cents": r["net_cents"], "vat_cents": r["vat_cents"], "total_cents": r["total_cents"]}


def load(oid):
    r = db.get().execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    if r is None:
        not_found(f"поръчка {oid}")
    return r


def _lines(raw, errors):
    """Редовете със снимка на цената; същият продукт два пъти → един ред."""
    if not isinstance(raw, list) or not raw:
        errors["lines"] = "непразен списък от {product_id, qty}"
        return []
    merged: dict = {}
    for item in raw:
        if not isinstance(item, dict) or not is_int(item.get("product_id")) \
                or not is_int(item.get("qty")) or item["qty"] < 1:
            errors["lines"] = "всеки ред е {product_id: цяло, qty: цяло ≥ 1}"
            return []
        merged[item["product_id"]] = merged.get(item["product_id"], 0) + item["qty"]
    con = db.get()
    lines = []
    for pid, qty in merged.items():
        p = con.execute("SELECT name, price_cents FROM products WHERE id = ?", (pid,)).fetchone()
        if p is None:
            errors["lines"] = f"няма продукт {pid}"
            return []
        lines.append({"product_id": pid, "name": p["name"], "qty": qty,
                      "unit_price_cents": p["price_cents"], "net_cents": qty * p["price_cents"]})
    return lines


@bp.post("/orders")
def create():
    body = json_object()
    errors = {}
    cid = body.get("customer_id")
    con = db.get()
    if not is_int(cid) or con.execute("SELECT 1 FROM customers WHERE id = ?", (cid,)).fetchone() is None:
        errors["customer_id"] = "няма такъв клиент"
    lines = _lines(body.get("lines"), errors)
    if errors:
        fail(errors)
    t = totals(lines)
    with con:
        oid = con.execute("INSERT INTO orders (customer_id, status, net_cents, vat_cents, total_cents) "
                          "VALUES (?, 'new', ?, ?, ?)",
                          (cid, t["net_cents"], t["vat_cents"], t["total_cents"])).lastrowid
        for pos, ln in enumerate(lines):
            con.execute("INSERT INTO order_lines VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (oid, pos, ln["product_id"], ln["name"], ln["qty"], ln["unit_price_cents"],
                         ln["net_cents"]))
    return jsonify(to_json(load(oid))), 201


@bp.get("/orders")
def listing():
    pg = pagination()
    where, args = ["1"], []
    status = request.args.get("status")
    if status is not None:
        if status not in STATUSES:
            fail({"status": f"едно от {', '.join(STATUSES)}"})
        where.append("status = ?")
        args.append(status)
    customer = request.args.get("customer_id")
    if customer is not None:
        try:
            args.append(int(customer))
        except ValueError:
            fail({"customer_id": "цяло число"})
        where.append("customer_id = ?")
    return jsonify(db.page(f"orders WHERE {' AND '.join(where)}", args, pg, to_json))


@bp.get("/orders/<int:oid>")
def one(oid):
    return jsonify(to_json(load(oid)))


@bp.post("/orders/<int:oid>/status")
def change_status(oid):
    r = load(oid)
    new = json_object().get("status")
    if new not in STATUSES:
        fail({"status": f"едно от {', '.join(STATUSES)}"})
    if (r["status"], new) not in ALLOWED:
        conflict(f"не може от {r['status']} към {new}")
    con = db.get()
    with con:
        con.execute("UPDATE orders SET status = ? WHERE id = ?", (new, oid))
    return jsonify(to_json(load(oid)))
