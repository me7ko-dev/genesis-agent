import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                                     price_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                                      email TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL,
                                   status TEXT NOT NULL, net_cents INTEGER NOT NULL,
                                   vat_cents INTEGER NOT NULL, total_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS order_lines (order_id INTEGER NOT NULL, pos INTEGER NOT NULL,
                                        product_id INTEGER NOT NULL, name TEXT NOT NULL,
                                        qty INTEGER NOT NULL, unit_price_cents INTEGER NOT NULL,
                                        net_cents INTEGER NOT NULL, PRIMARY KEY (order_id, pos));
"""


def init(app):
    con = sqlite3.connect(app.config["DB_PATH"])
    con.executescript(SCHEMA)
    con.close()
    app.teardown_appcontext(close)


def get():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DB_PATH"])
        g.db.row_factory = sqlite3.Row
    return g.db


def close(_exc=None):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def page(sql_from, args, pg, to_json):
    """{"items", "page", "per_page", "total"} за SELECT ... FROM <sql_from>, по id."""
    con = get()
    total = con.execute(f"SELECT COUNT(*) FROM {sql_from}", args).fetchone()[0]
    page_no, per_page = pg
    rows = con.execute(f"SELECT * FROM {sql_from} ORDER BY id LIMIT ? OFFSET ?",
                       [*args, per_page, (page_no - 1) * per_page]).fetchall()
    return {"items": [to_json(r) for r in rows], "page": page_no, "per_page": per_page, "total": total}
