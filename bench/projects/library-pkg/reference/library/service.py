from datetime import date, timedelta

from .errors import LimitReached, NotAvailable, NotFound
from .models import LOAN_DAYS, MAX_LOANS, Book, Loan, normalize_isbn
from .storage import Storage


class Library:
    def __init__(self, db_path):
        self.db = Storage(db_path)

    def _book(self, isbn):
        b = self.db.book(normalize_isbn(isbn))
        if b is None:
            raise NotFound(f"Няма книга {isbn}")
        return b

    def add_book(self, isbn, title, author, copies=1):
        isbn = normalize_isbn(isbn)
        b = self.db.book(isbn)
        self.db.save_book(Book(isbn, title, author, copies + (b.copies if b else 0)))

    def add_reader(self, name):
        return self.db.add_reader(name)

    def available(self, isbn):
        b = self._book(isbn)
        return b.copies - len(self.db.loans(isbn=b.isbn))

    def borrow(self, reader_id, isbn, on=None):
        on = on or date.today()
        if not self.db.reader_exists(reader_id):
            raise NotFound(f"Няма читател {reader_id}")
        b = self._book(isbn)
        mine = self.db.loans(reader_id=reader_id)
        if any(loan.isbn == b.isbn for loan in mine):
            raise NotAvailable("Читателят вече има тази книга")
        if len(mine) >= MAX_LOANS:
            raise LimitReached(f"До {MAX_LOANS} книги едновременно")
        if self.available(b.isbn) <= 0:
            raise NotAvailable("Няма свободна бройка")
        self.db.add_loan(Loan(reader_id, b.isbn, on))

    def return_book(self, reader_id, isbn, on=None):
        on = on or date.today()
        isbn = normalize_isbn(isbn)
        loans = self.db.loans(reader_id=reader_id, isbn=isbn)
        if not loans:
            raise NotFound("Читателят няма тази книга")
        self.db.delete_loan(reader_id, isbn)
        return max(0, (on - (loans[0].taken + timedelta(days=LOAN_DAYS))).days)

    def overdue(self, today):
        late = [(loan.reader_id, loan.isbn, (today - loan.taken - timedelta(days=LOAN_DAYS)).days)
                for loan in self.db.loans()]
        return sorted((x for x in late if x[2] > 0), key=lambda x: (-x[2], x[0], x[1]))

    def books(self):
        return [(b, b.copies - len(self.db.loans(isbn=b.isbn))) for b in self.db.books()]
