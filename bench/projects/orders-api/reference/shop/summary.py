from flask import Blueprint, jsonify

from . import db
from .orders import STATUSES

bp = Blueprint("summary", __name__)


@bp.get("/summary")
def summary():
    con = db.get()
    by_status = dict.fromkeys(STATUSES, 0)
    for status, n in con.execute("SELECT status, COUNT(*) FROM orders GROUP BY status"):
        by_status[status] = n
    revenue = con.execute("SELECT COALESCE(SUM(total_cents), 0) FROM orders "
                          "WHERE status IN ('paid', 'shipped')").fetchone()[0]
    top = con.execute(
        "SELECT l.product_id, p.name, SUM(l.qty) AS qty FROM order_lines l "
        "JOIN orders o ON o.id = l.order_id JOIN products p ON p.id = l.product_id "
        "WHERE o.status IN ('paid', 'shipped') GROUP BY l.product_id "
        "ORDER BY qty DESC, l.product_id LIMIT 3").fetchall()
    return jsonify({
        "orders": sum(n for s, n in by_status.items() if s != "cancelled"),
        "by_status": by_status,
        "revenue_cents": revenue,
        "top_products": [{"product_id": r["product_id"], "name": r["name"], "qty": r["qty"]} for r in top],
    })
