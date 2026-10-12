import sqlite3
from datetime import date

from .models import Book, Loan

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (isbn TEXT PRIMARY KEY, title TEXT, author TEXT, copies INTEGER);
CREATE TABLE IF NOT EXISTS readers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);
CREATE TABLE IF NOT EXISTS loans (reader_id INTEGER, isbn TEXT, taken TEXT, PRIMARY KEY (reader_id, isbn));
"""


class Storage:
    def __init__(self, path):
        self.con = sqlite3.connect(path)
        self.con.executescript(SCHEMA)

    def book(self, isbn):
        r = self.con.execute("SELECT isbn, title, author, copies FROM books WHERE isbn = ?", (isbn,)).fetchone()
        return Book(*r) if r else None

    def books(self):
        return [Book(*r) for r in self.con.execute("SELECT isbn, title, author, copies FROM books ORDER BY title")]

    def save_book(self, b):
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO books VALUES (?, ?, ?, ?)", (b.isbn, b.title, b.author, b.copies))

    def add_reader(self, name):
        with self.con:
            return self.con.execute("INSERT INTO readers (name) VALUES (?)", (name,)).lastrowid

    def reader_exists(self, rid):
        return self.con.execute("SELECT 1 FROM readers WHERE id = ?", (rid,)).fetchone() is not None

    def loans(self, reader_id=None, isbn=None):
        q, args = "SELECT reader_id, isbn, taken FROM loans WHERE 1", []
        if reader_id is not None:
            q, args = q + " AND reader_id = ?", args + [reader_id]
        if isbn is not None:
            q, args = q + " AND isbn = ?", args + [isbn]
        return [Loan(r, i, date.fromisoformat(t)) for r, i, t in self.con.execute(q, args)]

    def add_loan(self, loan):
        with self.con:
            self.con.execute("INSERT INTO loans VALUES (?, ?, ?)", (loan.reader_id, loan.isbn, loan.taken.isoformat()))

    def delete_loan(self, reader_id, isbn):
        with self.con:
            self.con.execute("DELETE FROM loans WHERE reader_id = ? AND isbn = ?", (reader_id, isbn))
