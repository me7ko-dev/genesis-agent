"""Еталонно решение — доказва, че скритите тестове се минават. Genesis не го вижда."""
import html
import re
import sqlite3
from datetime import date

from flask import Flask, jsonify, redirect, request

SERVICES = ["Подстригване", "Боядисване", "Прическа"]
SLOTS = [f"{h:02d}:{m:02d}" for h in range(9, 18) for m in (0, 30)]
_MOBILE = re.compile(r"(?:\+359|0)8[789]\d{7}")


def _page(values=None, error=""):
    v = {k: html.escape(x, quote=True) for k, x in (values or {}).items()}
    opts = "".join(f'<option{" selected" if v.get("service") == s else ""}>{s}</option>' for s in SERVICES)
    err = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="bg"><head><meta charset="utf-8"><title>Запиши час</title></head>
<body><h1>Запиши час</h1>{err}<form method="post" action="/book">
<label for="name">Име</label><input id="name" name="name" value="{v.get('name', '')}" required>
<label for="phone">Телефон</label><input id="phone" name="phone" value="{v.get('phone', '')}" required>
<label for="date">Дата</label><input id="date" type="date" name="date" value="{v.get('date', '')}" required>
<label for="time">Час</label><input id="time" type="time" name="time" step="1800" value="{v.get('time', '')}" required>
<label for="service">Услуга</label><select id="service" name="service" required><option value=""></option>{opts}</select>
<button type="submit">Запиши</button></form></body></html>"""


def create_app(db_path):
    app = Flask(__name__)

    def db():
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE IF NOT EXISTS bookings (name TEXT, phone TEXT, day TEXT, slot TEXT,"
                    " service TEXT, UNIQUE(day, slot))")
        return con

    def taken(day):
        with db() as con:
            return {r[0] for r in con.execute("SELECT slot FROM bookings WHERE day = ?", (day,))}

    @app.get("/")
    def index():
        return _page()

    @app.post("/book")
    def book():
        f = {k: request.form.get(k, "").strip() for k in ("name", "phone", "date", "time", "service")}
        error = ""
        try:
            day = date.fromisoformat(f["date"])
        except ValueError:
            day = None
        if not all(f.values()):
            error = "Попълни всички полета."
        elif not _MOBILE.fullmatch(re.sub(r"[\s/-]", "", f["phone"])):
            error = "Телефонът трябва да е български мобилен номер."
        elif day is None or day < date.today():
            error = "Датата трябва да е днес или по-късно."
        elif f["time"] not in SLOTS:
            error = "Часът трябва да е между 09:00 и 17:30, през половин час."
        elif f["service"] not in SERVICES:
            error = "Избери услуга от списъка."
        elif f["time"] in taken(f["date"]):
            error = "Този час вече е зает."
        if error:
            return _page(f, error), 400
        try:
            with db() as con:
                con.execute("INSERT INTO bookings VALUES (?, ?, ?, ?, ?)",
                            (f["name"], f["phone"], f["date"], f["time"], f["service"]))
        except sqlite3.IntegrityError:
            return _page(f, "Този час вече е зает."), 400
        return redirect("/thanks", code=303)

    @app.get("/thanks")
    def thanks():
        return '<!doctype html><html lang="bg"><body><h1>Благодарим!</h1><a href="/">Назад</a></body></html>'

    @app.get("/api/slots")
    def slots():
        busy = taken(request.args.get("date", ""))
        return jsonify([s for s in SLOTS if s not in busy])

    return app
