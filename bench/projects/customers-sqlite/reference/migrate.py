"""Еталонно решение — доказва, че скритите тестове се минават. Genesis не го вижда."""
import csv
import re
import sqlite3
import sys
from datetime import date

COLUMNS = ["Код", "Име", "Телефон", "Имейл", "Град", "Регистриран"]


def phone(raw):
    s = re.sub(r"[^\d+]", "", raw or "")
    if s.startswith("00"):
        s = "+" + s[2:]
    if s.startswith("+359"):
        rest = s[4:]
    elif s.startswith("359"):
        rest = s[3:]
    elif s.startswith("0"):
        rest = s[1:]
    else:
        return None
    return f"+359{rest}" if rest.isdigit() and 8 <= len(rest) <= 9 else None


def migrate(csv_path, db_path):
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE IF NOT EXISTS customers (code TEXT PRIMARY KEY, name TEXT NOT NULL,"
                " phone TEXT, email TEXT, city TEXT, registered TEXT)")
    stats = {"inserted": 0, "updated": 0, "skipped": 0}
    with open(csv_path, encoding="cp1251", newline="") as f:
        for row in csv.DictReader(f):
            v = {k: ((row.get(k) or "").strip() or None) for k in COLUMNS}
            if not any(v.values()):
                continue                       # empty line at the end
            try:
                d, m, y = (v["Регистриран"] or "").split(".")
                day = date(int(y), int(m), int(d)).isoformat()
            except ValueError:
                day = None
            if not v["Код"] or not v["Име"] or not day:
                stats["skipped"] += 1
                continue
            exists = con.execute("SELECT 1 FROM customers WHERE code = ?", (v["Код"],)).fetchone()
            con.execute("INSERT OR REPLACE INTO customers VALUES (?, ?, ?, ?, ?, ?)",
                        (v["Код"], v["Име"], phone(v["Телефон"]), v["Имейл"], v["Град"], day))
            stats["updated" if exists else "inserted"] += 1
    con.commit()
    con.close()
    return stats


if __name__ == "__main__":
    s = migrate(sys.argv[1], sys.argv[2])
    print(f"нови: {s['inserted']}, обновени: {s['updated']}, пропуснати: {s['skipped']}")
